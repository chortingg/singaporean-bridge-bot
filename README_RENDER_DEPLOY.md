# BridgeBot Render deployment

This is the current BridgeBot build with webhook support for Render.

## Files to push to GitHub

- `bridgebot_fixed.py`
- `requirements_bridgebot.txt`
- `render.yaml`

## Render settings

Create a Render **Web Service** from the GitHub repository.

Build command:

```bash
pip install -r requirements_bridgebot.txt
```

Start command:

```bash
python bridgebot_fixed.py
```

Environment variables:

```text
TELEGRAM_BOT_TOKEN=your BotFather token
BRIDGEBOT_MODE=webhook
WEBHOOK_PATH=telegram
WEBHOOK_URL=https://your-render-service-name.onrender.com
```

Render supplies `PORT`; the bot binds to it automatically.

## Telegram setting

Reply-keyboard card play sends normal group text, so disable BotFather privacy mode:

```text
/setprivacy
choose your bot
Disable
```

## Notes

- Local mode still works when `BRIDGEBOT_MODE` is unset or set to `local`/`polling`.
- Render Free can spin down after inactivity, so the first action after idle can be delayed.
- Local JSON state on free web services is ephemeral. Finish games in one session, or add persistent storage later.
