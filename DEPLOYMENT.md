# Friday Jarvis - Deployment Guide

## Railway.app Deployment

This guide explains how to deploy Friday Jarvis to Railway.app using Docker.

### Prerequisites

- Railway.app account
- GitHub repository connected to Railway
- All environment variables configured in Railway dashboard

### Required Environment Variables

Ensure the following variables are set in Railway's Variable Services:

```
LIVEKIT_URL=
LIVEKIT_API_KEY=
LIVEKIT_API_SECRET=
GOOGLE_API_KEY=
GMAIL_APP_PASSWORD=
GMAIL_USER=
MEM0_API_KEY=
OPENAI_API_KEY=
N8N_MCP_SERVER_URL=
```

### Deployment Steps

#### 1. Using Railway Dashboard

1. Go to [Railway.app](https://railway.app)
2. Create a new project
3. Connect your GitHub repository (gelson12/friday_jarvis2)
4. Select the `br` branch for deployment
5. Railway will automatically detect the Dockerfile
6. Add all environment variables in the "Variables" tab
7. Deploy the application

#### 2. Using Railway CLI

```bash
# Install Railway CLI if not already installed
npm install -g @railway/cli

# Login to Railway
railway login

# Navigate to your project directory
cd friday_jarvis2

# Create a new project
railway init

# Link to your GitHub repo
railway link

# Set environment variables
railway variables set LIVEKIT_URL=your_value
railway variables set LIVEKIT_API_KEY=your_value
# ... repeat for all variables

# Deploy
railway up
```

### Build Details

- **Base Image**: python:3.11-slim
- **Working Directory**: /app
- **Python Version**: 3.11
- **Start Command**: `python -m agent`

### System Dependencies

The Dockerfile includes:
- gcc, g++, build-essential for Python packages that need compilation
- git for any dependencies from git repos

### Environment Configuration

All environment variables are read from Railway's Variable Services. The application uses `python-dotenv` which will also read from a `.env` file if present (useful for local development).

### Health Checks

The Docker image includes a basic health check that runs every 30 seconds. This helps Railway monitor the application status.

### Restart Policy

The `railway.json` configuration includes:
- Auto-restart on failure
- Maximum 5 restart retries
- Type: on_failure

### Monitoring

1. Access logs through Railway dashboard: `Logs` tab
2. Monitor resource usage in the deployment settings
3. Check health status in the service details

### Local Development with Docker

To test the Docker setup locally before deploying to Railway:

```bash
# Build the image
docker build -t friday-jarvis:latest .

# Run with environment variables
docker run --env-file .env friday-jarvis:latest

# Or use docker-compose
docker-compose up --build
```

### Troubleshooting

#### Build Fails
- Check that requirements.txt is present and properly formatted
- Verify all Python packages are compatible with Python 3.11
- Review Railway build logs for specific error messages

#### Runtime Errors
- Ensure all environment variables are properly set in Railway
- Check application logs in Railway dashboard
- Verify that external services (LiveKit, N8N, etc.) are accessible

#### Performance Issues
- Monitor Railway resource usage
- Consider upgrading the Railway plan if CPU/Memory is maxed out
- Check for memory leaks in the application

### Useful Commands

```bash
# View logs
railway logs

# Access service shell
railway shell

# Check status
railway status

# Redeploy
railway trigger
```

### References

- [Railway Documentation](https://docs.railway.app)
- [Docker Documentation](https://docs.docker.com)
- [LiveKit Agents Documentation](https://docs.livekit.io/agents)
