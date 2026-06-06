# BridgeBot Repair Package

## What was recovered

`bridge.py` is a standalone Singaporean / floating bridge game-engine fragment. It implements:

- a 52-card deck and shuffling;
- bridge hand strength points and a wash/redeal condition;
- 35 ordered contracts from `1 ♣` through `7 🚫`;
- hand display, legal-suit enforcement, trump breaking, and trick comparison.

It contains no Telegram bot setup, handlers, lobby, player join flow, state storage, or token handling. The provided `bridge.cpython-39.pyc` disassembles to the same module names and functions and identifies its source as `C:\Users\chort\Downloads\bridge.py`, compiled on 13 February 2024. `access-bridge-64.jar` is a Java Accessibility Bridge archive, unrelated to the card bot. The referenced `silabser.sys` file was unavailable for inspection in this runtime.

## What has been rebuilt

`bridgebot_fixed.py` is a complete Telegram bot for Singaporean / floating bridge using `python-telegram-bot` 22.7.

### Gameplay implemented

- Group lobby with `/start` or `/newgame`.
- Private join button, so human players receive full hand updates in direct messages.
- Multiple group tables can run at once, and the same human Telegram account can sit in more than one table.
- Optional bot fillers: 1 human + 3 bots, 2 humans + 2 bots, or 3 humans + 1 bot.
- Automatic hand deal and original wash rule: any hand scoring 4 points or fewer triggers a redeal.
- Ordered bidding with Pass and contracts `1 ♣` to `7 🚫`, selected from the group turn prompt. During bidding, the current player also gets a selective group reply keyboard showing their hand as suit-first reference rows.
- All-pass automatic redeal.
- Winning bidder calls a hidden partner card from the group turn prompt. During partner selection, the declarer also gets the same selective hand-reference keyboard.
- Opening lead from the player left of declarer.
- Follow-suit validation and no leading trump before trump has been broken.
- Card play uses a selective group reply keyboard: during play, each human sees every remaining card as its own button. Illegal card taps are posted to the table, rejected by the bot, and left unregistered.
- Private trick-count updates for all human players after every completed trick.
- `/currenttrick`, `/roundcards`, and `/tablecards` show only the cards currently exposed in the live trick.
- Table-banter commands: `/whoseturnah`, `/fasterleh`, `/walao`, `/huatah`, and `/sorry_can_i_undo`.
- Funny Telegram-safe game codes such as `WALAOKOPI7` or `HUATQUEEN3` instead of plain serial numbers.
- Trick resolution, contract result, and session scorekeeping.
- `/next` for a further deal with the same players and continuing score.

### Repair and reliability work

- Current async `python-telegram-bot` application structure.
- Atomic JSON state persistence in `bridgebot_state.json`.
- Restart recovery: after reopening Python, players use `/hand` or remaining buttons to continue.
- Callback validation so only the correct current player can use the group turn controls.
- Deal-number protection: old buttons cannot act on a new deal.
- Table selectors for private `/hand`, `/status`, `/score`, `/leave`, and `/stop` when a player is in several games.
- Automatic Telegram command-menu registration on startup.
- No token hard-coded into source; token is read from the `TELEGRAM_BOT_TOKEN` environment variable.

## Files

- `bridgebot_fixed.py` — runnable repaired bot.
- `requirements_bridgebot.txt` — Python dependency version.
- `test_bridgebot_engine.py` — automated engine tests.

## Run it on your Windows laptop

Open Command Prompt in a folder containing the three files:

```cmd
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements_bridgebot.txt
set TELEGRAM_BOT_TOKEN=PASTE_YOUR_BOTFATHER_TOKEN_HERE
python bridgebot_fixed.py
```

Your laptop is the host. The bot is online while this Python process is running and your laptop has internet access. The active tables are stored in `bridgebot_state.json`, so restarting the program resumes saved games.

For PowerShell, use this token command instead:

```powershell
$env:TELEGRAM_BOT_TOKEN="PASTE_YOUR_BOTFATHER_TOKEN_HERE"
```

For a token that remains available after closing PowerShell, set it once as a user environment variable and open a new PowerShell window:

```powershell
[Environment]::SetEnvironmentVariable("TELEGRAM_BOT_TOKEN", "PASTE_YOUR_BOTFATHER_TOKEN_HERE", "User")
```

Keep that token private. A person holding it can control your bot.

## Use it in Telegram

1. Add the bot to a Telegram group.
2. In that group, send `/start`.
3. Human players tap **Join privately 🂡**, open the private bot chat, and join.
4. Once four seats are filled, private hands are delivered automatically. Bids and partner calls appear as group buttons. During bidding/calling, the current player also gets a selective hand-reference reply keyboard. During card play, each human sees every remaining card as an individual reply-keyboard button; the bot rejects illegal choices and keeps the move pending.
5. To play with fewer than four humans, the host sends `/bots` in the group lobby after at least one human has joined privately.

### Commands

| Command | Where | Purpose |
|---|---|---|
| `/start` or `/newgame` | Group/private | Open a new table or join through a deep link |
| `/status` | Group/private | Show current phase and next player |
| `/hand` | Private | Refresh your full hand and table status |
| `/score` | Group/private | Show session scores |
| `/bots` or `/fillbots` | Group lobby, host | Fill empty seats with simple bot players |
| `/partner`, `/call`, `/choosepartner` | Group/private | Redraw the partner-card selector if Telegram buries it |
| `/whoseturnah` or `/whose_turn_ah` | Group/private | Screams the current player's turn in caps |
| `/currenttrick`, `/roundcards`, `/tablecards` | Group/private | Shows only the cards played into the current live trick |
| `/fasterleh` | Group/private | Posts a deadpan hurry-up line aimed at the current player |
| `/walao` | Group/private | Posts a table-complaint line |
| `/huatah` | Group/private | Posts a chaotic blessing/encouragement line |
| `/sorry_can_i_undo` or `/undo` | Group/private | Performs fake undo theatre; game state is unchanged |
| `/next` | Group, host | Deal the next hand after a finished deal |
| `/leave` | Private | Leave before the table fills |
| `/stop` | Group/private, host | Close the table and remove saved game state |
| `/rules` | Anywhere | Display implemented rules |

For reply-keyboard card play, the bot must receive the card text message in the group. If your bot has BotFather privacy mode enabled and does not react to tapped card messages, disable privacy mode with `/setprivacy` in BotFather, then restart the bot.

### Group reply keyboard and private hands

Telegram inline keyboards attached to group messages are visible to the group. Card play therefore uses a selective reply keyboard instead. During play, the player's reply keyboard shows every remaining card as its own button. The table prompt stays short, for example `Tony, YOUR TURN.`

Bids and partner-call buttons remain inline group buttons. During bidding and partner selection, selective reply keyboards can contain suit-first hand-reference rows such as `♣ A K 7`. During card play, the keyboard switches to actual card buttons such as `A ♣`; tapping one sends it into the group as the attempted play. Full hand updates still go to private chat.

## Automated verification performed

```cmd
python -m unittest -v test_bridgebot_engine.py
```

Covered behaviours:

- complete 52-card unique deal and wash threshold;
- contract ordering;
- auction termination after three passes;
- trump trick comparison;
- follow-suit and trump-breaking restrictions;
- hidden partner ownership selection;
- full 13-trick simulated deal with scoring;
- rejection design for stale buttons after redeals;
- same human existing in two tables;
- trick-count display for every player;
- bot helper choices for bids, partner call, card play, and bot seat IDs;
- current-trick-only recall;
- stateless fake undo and turn-shout text;
- selective reply-keyboard card play with every remaining card shown as a button, bidding/call hand-reference keyboards, illegal-move rejection, and card-message parsing;
- funny Telegram-safe game-code generation.
