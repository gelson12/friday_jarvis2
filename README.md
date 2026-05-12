# 🧠 Friday - Your Personal AI Assistant - Part 2

A production-ready Python-based AI assistant inspired by *Jarvis* from Iron Man, fully integrated with **LiveKit** for real-time voice/video, **Hermes-Agent** for advanced capabilities, and **Mem0** for intelligent memory.

## 🎯 Key Features

- 🔍 **Web Search** - Real-time information retrieval via DuckDuckGo
- 🌤️ **Weather** - Current weather for any location
- 📨 **Email** - Send emails through Gmail
- 📷 **Vision** - Camera input through web app
- 🗣️ **Real-time Speech** - Crystal clear voice interactions via LiveKit
- 📝 **Chat** - Multi-turn conversations with web interface
- 🧠 **Smart Memory** - Persistent conversation memory with Mem0
- 🎵 **Spotify** - Music control and playlist management (via Hermes)
- 🔧 **MCP Integration** - Advanced capabilities through Hermes-Agent
- 🚀 **Production Ready** - Fully containerized with Docker

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Friday_jarvis2 System                      │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌──────────────────┐   ┌──────────────────┐                 │
│  │  LiveKit Server  │   │  Hermes Agent    │                 │
│  │  - WebRTC        │   │  - MCP Server    │                 │
│  │  - Media Stream  │   │  - Spotify       │                 │
│  │  - Room Mgmt     │   │  - N8N           │                 │
│  └────────┬─────────┘   └────────┬─────────┘                 │
│           │                      │                           │
│           └──────────┬───────────┘                           │
│                      │                                       │
│           ┌──────────▼──────────┐                            │
│           │  Friday Agent       │                            │
│           │  - OpenAI LLM       │                            │
│           │  - Tool Execution   │                            │
│           │  - Mem0 Memory      │                            │
│           │  - Context Mgmt     │                            │
│           └─────────────────────┘                            │
│                                                               │
└─────────────────────────────────────────────────────────────┘
```

## 🚀 Quick Start

### Option 1: Docker Compose (Recommended)

**Prerequisites:**
- Docker & Docker Compose installed
- Git

**Setup:**
```bash
# Clone repository
git clone https://github.com/gelson12/friday_jarvis2.git
cd friday_jarvis2
git checkout hermes-migration

# Configure environment
cp .env.example .env
# Edit .env with your API keys
nano .env

# Start all services
docker-compose up --build
```

**Services running:**
- LiveKit: `ws://localhost:7880`
- Hermes Agent: `http://localhost:8080`
- Friday Agent: Ready for connections

### Option 2: Railway Deployment

1. **Create account** at https://railway.app
2. **Link GitHub** to Railway
3. **Set environment variables** in Railway dashboard
4. **Deploy** - Railway automatically builds and starts your services

See [DEPLOYMENT.md](./DEPLOYMENT.md) for detailed Railway setup.

### Option 3: Manual Local Setup

```bash
# Clone and checkout branch
git clone https://github.com/gelson12/friday_jarvis2.git
cd friday_jarvis2
git checkout hermes-migration

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
nano .env

# Start LiveKit server (requires separate setup or Docker)
docker run --rm -p 7880:7880/tcp -p 7882:7882/udp \
  -e LIVEKIT_API_KEY=devkey \
  -e LIVEKIT_API_SECRET=secret \
  livekit/livekit-server:latest

# In another terminal: Start Hermes-Agent (if separate repo)
# cd ../Hermes-agent && python main.py

# In another terminal: Start Friday Agent
python -m livekit.agents agent.entrypoint
```

## 📋 Configuration

### Required Environment Variables

```env
# LiveKit
LIVEKIT_URL=ws://localhost:7880
LIVEKIT_API_KEY=your-api-key
LIVEKIT_API_SECRET=your-secret

# OpenAI
OPENAI_API_KEY=sk-...

# Mem0 Memory System
MEM0_API_KEY=your-mem0-key

# Gmail (optional)
GMAIL_USER=your-email@gmail.com
GMAIL_APP_PASSWORD=your-app-password

# Google
GOOGLE_API_KEY=your-google-key

# Hermes Integration
N8N_MCP_SERVER_URL=http://localhost:8080

# Optional: Spotify
SPOTIFY_CLIENT_ID=your-client-id
SPOTIFY_CLIENT_SECRET=your-client-secret
SPOTIFY_REDIRECT_URI=http://localhost:8888/callback
```

See `.env.example` for all configuration options with descriptions.

## 🔑 Getting API Keys

### OpenAI API Key
1. Visit https://platform.openai.com/api-keys
2. Create new API key
3. Add to `.env` as `OPENAI_API_KEY`

### Mem0 API Key
1. Sign up at https://app.mem0.ai
2. Generate API key in settings
3. Add to `.env` as `MEM0_API_KEY`

### Gmail App Password
1. Enable 2FA on your Gmail account
2. Visit https://myaccount.google.com/apppasswords
3. Generate 16-character app password
4. Add `GMAIL_USER` and `GMAIL_APP_PASSWORD` to `.env`

### LiveKit
- **Cloud**: Sign up at https://cloud.livekit.io
- **Self-hosted**: Deploy using Docker (included in docker-compose.yml)

### Spotify (Optional)
1. Create app at https://developer.spotify.com/dashboard
2. Get Client ID and Secret
3. Set Redirect URI in app settings
4. Add credentials to `.env`

## 📚 Documentation

- **[DEPLOYMENT.md](./DEPLOYMENT.md)** - Detailed deployment guide for Docker, Railway, and manual setup
- **[Tutorial Part 1](https://youtu.be/An4NwL8QSQ4)** - Voice agent setup (YouTube)
- **[Tutorial Part 2](https://www.youtube.com/watch?v=gqmSKEUpRv8)** - Memory and MCP server (YouTube)

## 🔧 Development

### Project Structure

```
friday_jarvis2/
├── agent.py                 # Main LiveKit agent
├── prompts.py              # System and session prompts
├── tools.py                # Built-in tools (weather, search, email)
├── mcp_client/             # MCP client for Hermes integration
│   ├── __init__.py
│   ├── agent_tools.py      # Tool integration for MCP
│   ├── server.py           # MCP server implementation
│   └── util.py             # Utility functions
├── Dockerfile              # Container for Friday Agent
├── docker-compose.yml      # Multi-service orchestration
├── requirements.txt        # Python dependencies
├── .env.example            # Environment template
└── DEPLOYMENT.md           # Deployment guide
```

### Adding New Tools

1. **Create tool function** in `tools.py`:
   ```python
   @function_tool()
   async def my_tool(context: RunContext, param: str) -> str:
       """Tool description."""
       # Implementation
       return result
   ```

2. **Add to agent** in `agent.py`:
   ```python
   tools=[
       get_weather,
       search_web,
       send_email,
       my_tool  # Add here
   ]
   ```

### Hermes Integration

Tools are dynamically loaded from **Hermes-Agent** via MCP (Model Context Protocol):

1. **Hermes** exposes tools via MCP server (port 8080)
2. **Friday** connects to Hermes using `MCPServerSse`
3. Tools are automatically available to the LLM

See [Hermes-Agent](https://github.com/your-org/Hermes-agent) repository for adding custom tools.

## 🚦 Health Checks

```bash
# Check service status
docker-compose ps

# Test LiveKit health
curl http://localhost:7880/health

# Test Hermes health
curl http://localhost:8080/health

# View logs
docker-compose logs -f friday-agent
```

## 📊 Monitoring & Logging

**Docker Compose:**
```bash
# Real-time logs
docker-compose logs -f

# Specific service
docker-compose logs -f friday-agent

# Last 100 lines
docker-compose logs --tail=100 friday-agent
```

**Production (Railway):**
- Monitor through Railway dashboard
- Set up alerting in Railway settings
- Configure log forwarding (Datadog, LogRocket, etc.)

## 🔐 Security

- ✅ Never commit `.env` with real keys to GitHub
- ✅ Use `.env` locally, environment variables in production
- ✅ Rotate API keys periodically
- ✅ Use app-specific passwords (Gmail, etc.)
- ✅ Enable 2FA on all service accounts
- ✅ Keep dependencies updated: `pip install --upgrade -r requirements.txt`

## 📦 Dependencies

Key packages (see `requirements.txt` for complete list):

- `livekit-agents` - LiveKit agent framework
- `livekit-plugins-openai` - OpenAI integration
- `pydantic-ai-slim[openai,mcp]` - AI framework with MCP support
- `mem0ai` - Memory system
- `duckduckgo-search` - Web search
- `langchain_community` - LLM tools
- `python-dotenv` - Environment management

## 🐛 Troubleshooting

### Services won't start
```bash
# Check logs
docker-compose logs

# Rebuild images
docker-compose up --build

# Clean and restart
docker-compose down -v
docker-compose up --build
```

### Connection refused errors
- Verify all services in docker-compose.ps are running
- Check `N8N_MCP_SERVER_URL` in `.env` matches service name
- For local development: use `localhost`, for Docker: use service name

### Memory not saving
- Verify `MEM0_API_KEY` is valid
- Check Mem0 account status at https://app.mem0.ai
- Review logs: `docker-compose logs friday-agent | grep -i mem0`

### OpenAI errors
- Verify API key is valid: https://platform.openai.com/api-keys
- Check account has credits/billing enabled
- Ensure model `gpt-4-realtime-preview` is available in your region

See [DEPLOYMENT.md](./DEPLOYMENT.md#troubleshooting) for more troubleshooting.

## 📄 License

**Proprietary & Open Source Components:**

- **Proprietary Code**: All files except `mcp_client` and portions of `agent.py` not authored by Thanh-Y Nguyen — Copyright © 2025 Thanh-Y Nguyen. Licensed for private/educational use only. Redistribution, publication, or commercial use is prohibited without written permission.

- **Third-party Components**:
  - `mcp_client` — Copyright © LiveKit, Inc., MIT License
  - Portions of `agent.py` not authored by Thanh-Y Nguyen — MIT or other applicable license
  - See `thirdparty/LICENSE-LIVEKIT` for details

## 🤝 Contributing

1. Fork the repository
2. Create feature branch: `git checkout -b feature/amazing-feature`
3. Commit changes: `git commit -m 'Add amazing feature'`
4. Push to branch: `git push origin feature/amazing-feature`
5. Open Pull Request

## 💬 Support

- **Issues**: https://github.com/gelson12/friday_jarvis2/issues
- **Discussions**: https://github.com/gelson12/friday_jarvis2/discussions
- **LiveKit Support**: https://docs.livekit.io
- **OpenAI Support**: https://platform.openai.com/docs

## 🎯 Roadmap

- [ ] Web UI improvements
- [ ] Advanced Spotify integration
- [ ] Custom knowledge base integration
- [ ] Multi-language support
- [ ] Advanced analytics
- [ ] Mobile app

## 🙏 Acknowledgments

- Built on [LiveKit](https://livekit.io) - Incredible open-source WebRTC platform
- Powered by [OpenAI](https://openai.com) - State-of-the-art LLM
- Memory system by [Mem0](https://mem0.ai)
- Original concept by Thanh-Y Nguyen

---

**Happy coding! 🚀**
