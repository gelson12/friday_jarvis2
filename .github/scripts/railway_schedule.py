"""Stop / restart Railway services on a schedule.

Called from GitHub Actions on cron. Authenticates with a Railway
account token (stored as the `RAILWAY_TOKEN` GitHub secret), looks up
the target services by name within the configured project, and calls
the right GraphQL mutation per `--action`.

Usage:
    python railway_schedule.py --action stop  --project <name> --services A,B,C
    python railway_schedule.py --action start --project <name> --services A,B,C

The "stop" action calls `deploymentStop` on each service's latest
deployment. The "start" action calls `serviceInstanceRedeploy` to spin
a fresh deployment of the latest commit. Both are idempotent — a
service already stopped is silently skipped; a service already
running is redeployed (which restarts it — acceptable as a no-op).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any

import httpx

RAILWAY_API = "https://backboard.railway.com/graphql/v2"


def gql(token: str, query: str, variables: dict[str, Any] | None = None) -> dict:
    """Send one GraphQL request to Railway's API. Raises on error."""
    r = httpx.post(
        RAILWAY_API,
        headers={"Authorization": f"Bearer {token}"},
        json={"query": query, "variables": variables or {}},
        timeout=30.0,
    )
    r.raise_for_status()
    body = r.json()
    if "errors" in body and body["errors"]:
        raise RuntimeError(f"Railway API error: {body['errors']}")
    return body["data"]


def find_project(token: str, project_name: str) -> dict:
    """Find a project by name + return its services + production environment ID."""
    data = gql(
        token,
        """
        query {
          me {
            projects {
              edges {
                node {
                  id
                  name
                  services {
                    edges {
                      node {
                        id
                        name
                      }
                    }
                  }
                  environments {
                    edges {
                      node {
                        id
                        name
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """,
    )
    for edge in data["me"]["projects"]["edges"]:
        proj = edge["node"]
        if proj["name"].lower() == project_name.lower():
            services = {
                e["node"]["name"]: e["node"]["id"]
                for e in proj["services"]["edges"]
            }
            envs = {
                e["node"]["name"].lower(): e["node"]["id"]
                for e in proj["environments"]["edges"]
            }
            env_id = envs.get("production") or next(iter(envs.values()))
            return {
                "project_id": proj["id"],
                "services": services,
                "environment_id": env_id,
            }
    raise RuntimeError(f"Project {project_name!r} not found in this account")


def latest_deployment(
    token: str, service_id: str, environment_id: str
) -> str | None:
    """Return the most recent deployment ID for a service, or None."""
    data = gql(
        token,
        """
        query($serviceId: String!, $environmentId: String!) {
          deployments(
            input: { serviceId: $serviceId, environmentId: $environmentId }
            first: 1
          ) {
            edges {
              node {
                id
                status
              }
            }
          }
        }
        """,
        {"serviceId": service_id, "environmentId": environment_id},
    )
    edges = data["deployments"]["edges"]
    if not edges:
        return None
    return edges[0]["node"]["id"]


def stop_deployment(token: str, deployment_id: str) -> None:
    gql(
        token,
        "mutation($id: String!) { deploymentStop(id: $id) }",
        {"id": deployment_id},
    )


def redeploy_service(
    token: str, service_id: str, environment_id: str
) -> None:
    gql(
        token,
        """
        mutation($serviceId: String!, $environmentId: String!) {
          serviceInstanceRedeploy(
            serviceId: $serviceId,
            environmentId: $environmentId
          )
        }
        """,
        {"serviceId": service_id, "environmentId": environment_id},
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--action", required=True, choices=("stop", "start"))
    ap.add_argument("--project", required=True, help="Railway project name")
    ap.add_argument(
        "--services",
        required=True,
        help="Comma-separated list of service names to act on",
    )
    args = ap.parse_args()

    token = os.environ.get("RAILWAY_TOKEN", "").strip()
    if not token:
        print("ERROR: RAILWAY_TOKEN env var not set.", file=sys.stderr)
        return 2

    target_names = [s.strip() for s in args.services.split(",") if s.strip()]
    print(f"[{args.action}] project={args.project!r}  targets={target_names}")

    try:
        proj = find_project(token, args.project)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"  project_id     = {proj['project_id']}")
    print(f"  environment_id = {proj['environment_id']}")
    print(f"  known services = {list(proj['services'].keys())}")

    failures = 0
    for name in target_names:
        # Tolerate case differences
        match = next(
            (n for n in proj["services"] if n.lower() == name.lower()),
            None,
        )
        if not match:
            print(f"  ! {name}: not found in project (skipping)")
            failures += 1
            continue
        svc_id = proj["services"][match]

        try:
            if args.action == "stop":
                dep_id = latest_deployment(token, svc_id, proj["environment_id"])
                if not dep_id:
                    print(f"  - {match}: no active deployment to stop")
                    continue
                stop_deployment(token, dep_id)
                print(f"  ✓ {match}: stopped (deployment {dep_id[:8]})")
            else:  # start
                redeploy_service(token, svc_id, proj["environment_id"])
                print(f"  ✓ {match}: redeploy triggered")
            # Small gap to avoid hammering Railway's API
            time.sleep(0.5)
        except Exception as exc:
            print(f"  ! {match}: {exc}")
            failures += 1

    return 1 if failures > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
