# 🗞️ Intelligent News Agent (INA)

> **Not just another RSS reader—an autonomous, agent-native news discovery engine.**  
> *INA learns what you like, filters out clickbait, creates local offline Markdown archives, and exposes an MCP server for your AI agents.*

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg)](https://fastapi.tiangolo.com/)
[![MCP](https://img.shields.io/badge/Protocol-FastMCP-purple.svg)](https://modelcontextprotocol.io/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

### ✨ Key Features

- **🤖 Agent-First Architecture:** Native FastMCP server exposes resources and tools for external agents (Hermes, OpenClaw, Claude Desktop) to search, fetch, and curate news.
- **🧠 Active Preference Learning:** Learns from your likes, dislikes, and soft-feedback to refine recommendations over time without trapping you in an echo chamber.
- **🛡️ Anti-Clickbait & Zero-Trust Security:** Rewrites sensationalized headlines and isolates untrusted web content to prevent Indirect Prompt Injection attacks.
- **📁 Local-First Markdown Archiving:** Downloads articles with full frontmatter metadata and local image assets directly into your filesystem.
- **🎛️ Flexible Consumption Modes:** Choose between real-time feeds, daily briefings, or a hybrid layout with a dedicated *Serendipity Slider*.
