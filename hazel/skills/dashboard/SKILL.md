---
name: dashboard
description: When the user wants to view, open, or access the dashboard, canvas, knowledge graph, intents calendar, or memory viewer — generate a secure time-limited link.
metadata: {"hazel":{"emoji":"","always":false}}
---

# Dashboard Access

Use the `dashboard_link` tool to give the user secure access to the Canvas dashboard.

## When to use

- User asks to "see the dashboard", "open canvas", "show me my entities/intents/memory"
- User asks for a "dashboard link" or "dashboard URL"
- User wants to visualize their knowledge graph, intents calendar, or daily logs

## How it works

1. Call `dashboard_link` (optional: `ttl_minutes` to control expiry, default 60)
2. The tool returns a URL with an embedded HMAC token
3. Send the URL to the user through the current channel
4. The link works for the configured TTL, then expires
5. When expired, the dashboard shows a message to ask you for a new link

## Example

User: "Can I see the dashboard?"

1. Call `dashboard_link` with default TTL
2. Send the returned URL to the user

## Notes

- Links are single-use in the sense that they expire — anyone with the link can access the dashboard until it expires
- The channel itself provides identity verification (e.g., Telegram allowFrom)
- If the user reports the link doesn't work, it may be a network issue (dashboard must be reachable from their device) — suggest they check `gateway.dashboard.host` and `gateway.dashboard.base_url` in config
