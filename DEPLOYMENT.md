# 🚀 Friday_jarvis2 + Hermes-Agent Deployment Guide

## Table of Contents
1. [Local Development with Docker](#local-development-with-docker)
2. [Railway Deployment](#railway-deployment)
3. [Manual Server Deployment](#manual-server-deployment)
4. [Architecture Overview](#architecture-overview)
5. [Troubleshooting](#troubleshooting)

---

## Local Development with Docker

### Prerequisites
- Docker & Docker Compose installed
- Git
- GitHub repository cloned: `git clone https://github.com/gelson12/friday_jarvis2.git`

### Quick Start

1. **Clone the repository and checkout the branch:**
   ```bash
   cd friday_jarvis2
   git checkout hermes-migration
   ```

2. **Set up environment variables:**
   ```bash
   cp .env.example .env
   # Edit .env with your API keys
   nano .env
   ```

3. **Build and start all services:**
   ```bash
   docker-compose up --build
   ```

   This will start:
   - **LiveKit Server** on `ws://localhost:7880`
   - **Hermes Agent (MCP Server)** on `http://localhost:8080`
   - **Friday Agent** (LiveKit Worker Agent)

4. **Verify services are running:**
   ```bash
   # Check container status
   docker-compose ps
   
   # View logs
   docker-compose logs -f friday-agent  # Friday logs
   docker-compose logs -f hermes-agent  # Hermes logs
   docker-compose logs -f livekit        # LiveKit logs
   ```

### Docker Compose Structure

```
┌─────────────────────────────────────────────────────────────┐
│                    Friday_jarvis2 Network                    │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌──────────────────┐   ┌──────────────────┐                 │
│  │  LiveKit Server  │   │  Hermes Agent    │                 │
│  │  (ws://7880)     │   │  (http://8080)   │                 │
│  │  - WebRTC        │   │  - MCP Server    │                 │
│  │  - Media Stream  │   │  - Spotify       │                 │
│  │  - Session Mgmt  │   │  - N8N Workflows │                 │
│  └────────┬─────────┘   └────────┬─────────┘                 │
│           │                      │                           │
│           └──────────┬───────────┘                           │
│                      │                                       │
│           ┌──────────▼──────────┐                            │
│           │  Friday Agent       │                            │
│           │  (LiveKit Worker)   │                            │
│           │  - LLM (OpenAI)     │                            │
│           │  - Memory (Mem0)    │                            │
│           │  - Tools Integration│                            │
│           └─────────────────────┘                            │
│                                                               │
└─────────────────────────────────────────────────────────────┘
```

### Common Docker Compose Commands

```bash
# Start services in background
docker-compose up -d

# Stop services
docker-compose down

# Rebuild images
docker-compose up --build

# View specific service logs
docker-compose logs -f friday-agent

# Execute command in container
docker-compose exec friday-agent bash

# Start with optional N8N
docker-compose --profile optional up -d

# Clean up everything (volumes included)
docker-compose down -v
```

---

## Railway Deployment

### Prerequisites
- Railway account (https://railway.app)
- GitHub repository linked
- API keys stored securely

### Step 1: Create Railway Project

1. Go to https://railway.app/new
2. Click "Deploy from GitHub repo"
3. Select `gelson12/friday_jarvis2` repository
4. Grant Railway access to your repository

### Step 2: Configure Services

Create three separate services on Railway:

#### Service 1: LiveKit Server
```yaml
Service: livekit/livekit-server:latest
Port: 7880
Environment Variables:
  LIVEKIT_API_KEY: your-key
  LIVEKIT_API_SECRET: your-secret
  LIVEKIT_URL: wss://your-railway-domain.up.railway.app
```

#### Service 2: Hermes Agent
```yaml
Service: Custom (Docker)
Port: 8080
Build Command: docker build -f Hermes-agent/Dockerfile -t hermes-agent .
Environment Variables:
  MCP_SERVER_PORT: 8080
  OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
  SPOTIFY_CLIENT_ID: ${{ secrets.SPOTIFY_CLIENT_ID }}
  SPOTIFY_CLIENT_SECRET: ${{ secrets.SPOTIFY_CLIENT_SECRET }}
  N8N_URL: your-n8n-url
```

#### Service 3: Friday Agent
```yaml
Service: Custom (Docker)
Port: 8081
Dockerfile: ./Dockerfile
Environment Variables:
  LIVEKIT_URL: wss://your-railway-domain.up.railway.app
  LIVEKIT_API_KEY: ${{ secrets.LIVEKIT_API_KEY }}
  LIVEKIT_API_SECRET: ${{ secrets.LIVEKIT_API_SECRET }}
  OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
  MEM0_API_KEY: ${{ secrets.MEM0_API_KEY }}
  N8N_MCP_SERVER_URL: http://hermes-agent:8080
  GMAIL_USER: ${{ secrets.GMAIL_USER }}
  GMAIL_APP_PASSWORD: ${{ secrets.GMAIL_APP_PASSWORD }}
```

### Step 3: Set Environment Variables in Railway

1. Go to your project settings
2. Add environment variables for each service:
   ```
   LIVEKIT_API_KEY
   LIVEKIT_API_SECRET
   OPENAI_API_KEY
   MEM0_API_KEY
   GMAIL_USER
   GMAIL_APP_PASSWORD
   SPOTIFY_CLIENT_ID
   SPOTIFY_CLIENT_SECRET
   ```

### Step 4: Deploy

```bash
# Railway will auto-deploy on push to main/hermes-migration
git push origin hermes-migration

# Or manually deploy through Railway dashboard
# Services → Deploy
```

### Step 5: Monitor Deployment

```bash
# Using Railway CLI
railway status
railway logs -s friday-agent
railway logs -s hermes-agent
railway logs -s livekit
```

---

## Manual Server Deployment

### Option A: Ubuntu/Debian Server with Docker

```bash
# 1. SSH into your server
ssh user@your-server.com

# 2. Install Docker & Docker Compose
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# 3. Clone repository
git clone https://github.com/gelson12/friday_jarvis2.git
cd friday_jarvis2
git checkout hermes-migration

# 4. Set up environment
cp .env.example .env
# Edit .env with production keys
nano .env

# 5. Start services
docker-compose up -d

# 6. Set up reverse proxy (Nginx)
# See Nginx configuration below
```

### Option B: Traditional Python Virtual Environment

```bash
# 1. Clone repository
git clone https://github.com/gelson12/friday_jarvis2.git
cd friday_jarvis2
git checkout hermes-migration

# 2. Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment
cp .env.example .env
nano .env

# 5. Install Hermes dependencies (if deploying separately)
cd ../Hermes-agent
pip install -r requirements.txt

# 6. Start services (requires LiveKit server running separately)
# Terminal 1: Hermes Agent
python -m hermes

# Terminal 2: Friday Agent
cd ../friday_jarvis2
python -m livekit.agents agent.entrypoint
```

### Nginx Reverse Proxy Configuration

```nginx
# /etc/nginx/sites-available/friday.conf

upstream livekit {
    server localhost:7880;
}

upstream hermes {
    server localhost:8080;
}

upstream friday {
    server localhost:8081;
}

server {
    listen 80;
    server_name your-domain.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    # LiveKit WebSocket
    location /livekit/ {
        proxy_pass http://livekit/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }

    # Hermes MCP Server
    location /hermes/ {
        proxy_pass http://hermes/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # Friday Agent
    location / {
        proxy_pass http://friday/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## Architecture Overview

### Component Interaction

```
┌─────────────────┐
│   Client/App    │
│  (Web Browser)  │
└────────┬────────┘
         │ WebSocket
         │ (Audio/Video)
         ▼
┌──────────────────────────────────┐
│      LiveKit Server              │
│  - Media Routing                 │
│  - Session Management            │
│  - Recording (Optional)          │
└──────────────┬───────────────────┘
               │ RPC
               ▼
┌──────────────────────────────────┐     ┌──────────────────────┐
│   Friday Agent (LiveKit Worker)  │────▶│  Hermes Agent        │
│  - OpenAI LLM                    │     │  - MCP Server        │
│  - Tool Execution                │     │  - Spotify Integration
│  - Mem0 Memory System            │     │  - N8N Workflows     │
│  - Session Context               │     │  - Custom Tools      │
└──────────────────────────────────┘     └──────────────────────┘
```

### Data Flow

1. **User connects** to LiveKit room via client app
2. **LiveKit server** detects participant, spawns Friday agent
3. **Friday agent** receives audio from client
4. **OpenAI LLM** processes request
5. **Friday** may call **Hermes** for advanced operations (Spotify, workflows)
6. **Response** sent back through LiveKit to client

---

## Troubleshooting

### Common Issues

#### 1. "Cannot connect to LiveKit"
```bash
# Check if LiveKit is running
docker-compose ps livekit

# Check LiveKit logs
docker-compose logs livekit

# Verify URL matches service name
# In docker-compose: ws://livekit:7880
# Local development: ws://localhost:7880
```

#### 2. "Hermes connection failed"
```bash
# Check Hermes health
curl http://localhost:8080/health

# Check logs
docker-compose logs hermes-agent

# Verify N8N_MCP_SERVER_URL in .env
# Should be: http://hermes-agent:8080 (docker) or http://localhost:8080 (local)
```

#### 3. "OpenAI API key invalid"
```bash
# Verify key in .env
grep OPENAI_API_KEY .env

# Test key validity
curl https://api.openai.com/v1/models \
  -H "Authorization: Bearer $OPENAI_API_KEY"
```

#### 4. "Memory not persisting (Mem0)"
```bash
# Verify Mem0 API key
grep MEM0_API_KEY .env

# Check Mem0 status
docker-compose logs friday-agent | grep -i mem0
```

#### 5. "Port already in use"
```bash
# Find what's using the port (example: 7880)
lsof -i :7880

# Kill the process
kill -9 <PID>

# Or change port in docker-compose.yml
```

### Debug Mode

```bash
# Run with debug logging
docker-compose env LOG_LEVEL=DEBUG up -f friday-agent

# Run in interactive mode
docker-compose run --rm friday-agent bash

# Inside container, test connections
curl http://hermes-agent:8080/health
curl -s ws://livekit:7880/health
```

---

## Production Checklist

- [ ] All API keys stored in environment variables (not in code)
- [ ] `.env` file added to `.gitignore`
- [ ] LiveKit configured with proper authentication
- [ ] HTTPS/WSS enabled for all connections
- [ ] Database backups configured (if using persistent storage)
- [ ] Logging and monitoring set up
- [ ] Error alerting configured
- [ ] Rate limiting enabled
- [ ] Cost monitoring for API calls (OpenAI, Mem0, etc.)
- [ ] Regular security audits
- [ ] Auto-scaling configured (if using Kubernetes)

---

## Support & Resources

- **LiveKit Docs**: https://docs.livekit.io
- **OpenAI API Docs**: https://platform.openai.com/docs
- **Mem0 Docs**: https://docs.mem0.ai
- **Railway Docs**: https://docs.railway.app
- **Docker Docs**: https://docs.docker.com

---

## Questions?

Create an issue: https://github.com/gelson12/friday_jarvis2/issues
