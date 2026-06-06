"""
BridgeBot: a Telegram bot for four-player Singaporean / floating bridge.

Recovered from the user's original bridge.py card engine and rebuilt around
python-telegram-bot v22.7. The bot is intentionally button-driven:
- A game is created in a Telegram group.
- Each player joins through a private-chat deep link and receives their hand privately.
- Hands stay private in direct messages.
- Bids and partner calls are selected from the group turn prompt.
- Card play uses a selective group reply keyboard that shows every remaining card.
- State is saved to JSON so a laptop restart can resume an unfinished session.

Run:
    pip install -r requirements_bridgebot.txt
    set TELEGRAM_BOT_TOKEN=YOUR_TOKEN_HERE         # Windows cmd
    $env:TELEGRAM_BOT_TOKEN="YOUR_TOKEN_HERE"     # PowerShell
    python bridgebot_fixed.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import string
from html import escape
from dataclasses import asdict, dataclass, field
from pathlib import Path
from random import SystemRandom
from typing import Any, Iterable, Optional

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Game configuration
# ---------------------------------------------------------------------------

PLAYERS = 4
CARD_SUITS = ["♣", "♦", "♥", "♠"]
BID_SUITS = ["♣", "♦", "♥", "♠", "🚫"]
VALUES_ASC = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
VALUES_DESC = list(reversed(VALUES_ASC))

# The recovered source washes if get_points(hand) <= 4. Preserved here.
WASH_IF_POINTS_AT_MOST = int(os.getenv("BRIDGEBOT_WASH_THRESHOLD", "4"))

STATE_FILE = Path(os.getenv("BRIDGEBOT_STATE_FILE", "bridgebot_state.json"))
_rng = SystemRandom()

GAME_CODE_PREFIXES = [
    "WALAO", "HUAT", "KANJIONG", "BLUR", "STEADY", "CHOPE", "SIAO", "PAISEH",
    "SHIOK", "KAYPOH", "ALAMAK", "JIALAT", "POWER", "LEPAK", "CHIONG",
]
GAME_CODE_NOUNS = [
    "KING", "QUEEN", "CRAB", "OTTER", "KOPI", "PRATA", "TRUMP", "DIAMOND",
    "SPADE", "HEART", "CLUB", "BID", "SLIPPER", "MANTOU", "GONG",
]

TURN_EMOJIS = ["🚨", "🂡", "📣", "⚡", "👑", "🦦"]

WALAO_LINES = [
    "Walao. The table has entered its committee meeting phase.",
    "Walao eh. Four people, fifty-two cards, one national productivity crisis.",
    "Walao. Somewhere, a perfectly good trick is waiting for governance.",
    "Walao. The cards are ageing in place.",
    "Walao. This silence has received tenure.",
    "Walao. Even the bots are pretending to check their calendar.",
]

FASTER_LINES = [
    "{name}, it is your turn. The nation requests movement. 🫡",
    "{name}, faster leh. The trick has applied for permanent residency. 🕰️",
    "{name}, your card is awaited by scholars, aunties, and one disappointed bot. 🂡",
    "{name}, the table is experiencing a turn-based traffic jam. 🚦",
    "{name}, please proceed. The suspense is paying rent now. 🏚️",
    "{name}, your move. The cards have started a support group. 🃏",
]

HUATAH_LINES = [
    "HUAT AH. May your finesse survive contact with reality. 🧧",
    "HUAT AH. Statistically bold, spiritually questionable. 🍍",
    "HUAT AH. The table has chosen optimism over evidence. 🧮",
    "HUAT AH. Contract confidence has been deployed. 🚀",
    "HUAT AH. May the correct partner reveal at the least cursed moment. 🐉",
    "HUAT AH. Fortune favours the player who still remembers the trump suit. 🂱",
]

UNDO_LINES = [
    "SIKE. No undo allowed. Life has no Ctrl+Z, and neither does this table. — Albert Einstein, probably during a bad 3NT",
    "You thought you could take that back? Charming. The card has entered the public record. — Sun Tzu, after losing a queen",
    "Undo denied. Actions have consequences, which is why bridge was invented before customer service. — Anonymous uncle at the void deck",
    "Request received, laminated, and rejected. The past remains annoyingly installed. — Isaac Newton, after gravity clicked wrong",
    "No undo. The arrow of time continues to be rude. — Local thermodynamics department",
    "A card once played becomes a historical document. Historians are already disappointed. — National Archive of Questionable Decisions",
    "Undo? In this economy? The table has unanimously chosen accountability. — Very serious committee",
    "The universe considered your undo request and replied: lol. — Cosmology, 13.8 billion years running",
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Player:
    user_id: int
    name: str
    is_bot: bool = False


@dataclass
class PlayedCard:
    user_id: int
    value: str
    suit: str

    @property
    def label(self) -> str:
        return f"{self.value} {self.suit}"


@dataclass
class Game:
    game_id: str
    group_chat_id: int
    group_title: str
    host_id: int
    players: list[Player] = field(default_factory=list)
    phase: str = "lobby"  # lobby, bidding, calling, playing, finished
    hands: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    deal_number: int = 0
    dealer_pos: int = 0
    turn_pos: int = 0
    bid_history: list[dict[str, Any]] = field(default_factory=list)
    highest_bid: Optional[int] = None
    highest_bidder_id: Optional[int] = None
    passes_in_row: int = 0
    contract_bid: Optional[int] = None
    declarer_id: Optional[int] = None
    trump_suit: Optional[str] = None
    called_card: Optional[str] = None
    partner_id: Optional[int] = None
    partner_revealed: bool = False
    trick_number: int = 0
    current_trick: list[PlayedCard] = field(default_factory=list)
    trump_broken: bool = False
    tricks_won: dict[str, int] = field(default_factory=dict)
    score: dict[str, int] = field(default_factory=dict)
    result_text: Optional[str] = None

    def player_ids(self) -> list[int]:
        return [p.user_id for p in self.players]

    def position_of(self, user_id: int) -> int:
        return self.player_ids().index(user_id)

    def player(self, user_id: int) -> Player:
        return self.players[self.position_of(user_id)]

    def current_player(self) -> Player:
        return self.players[self.turn_pos]

    def player_name(self, user_id: int) -> str:
        return self.player(user_id).name

    def is_bot(self, user_id: int) -> bool:
        return self.player(user_id).is_bot

    def hand(self, user_id: int) -> dict[str, list[str]]:
        return self.hands[str(user_id)]

    def tricks(self, user_id: int) -> int:
        return self.tricks_won.get(str(user_id), 0)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.games: dict[str, Game] = {}
        self.group_to_game: dict[str, str] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.group_to_game = raw.get("group_to_game", {})
            for game_id, data in raw.get("games", {}).items():
                data["players"] = [Player(**p) for p in data.get("players", [])]
                data["current_trick"] = [PlayedCard(**c) for c in data.get("current_trick", [])]
                self.games[game_id] = Game(**data)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            LOGGER.exception("Could not load state file %s: %s", self.path, exc)
            backup = self.path.with_suffix(self.path.suffix + ".broken")
            try:
                self.path.replace(backup)
                LOGGER.error("Unreadable state moved to %s", backup)
            except OSError:
                pass

    def save(self) -> None:
        payload = {
            "games": {game_id: asdict(game) for game_id, game in self.games.items()},
            "group_to_game": self.group_to_game,
        }
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.path)

    def game_for_group(self, chat_id: int) -> Optional[Game]:
        game_id = self.group_to_game.get(str(chat_id))
        return self.games.get(game_id) if game_id else None

    def game_for_player(self, user_id: int, active_only: bool = True) -> Optional[Game]:
        matches = self.games_for_player(user_id, active_only=active_only)
        if not matches:
            return None
        return matches[-1]

    def games_for_player(self, user_id: int, active_only: bool = True) -> list[Game]:
        matches = [g for g in self.games.values() if user_id in g.player_ids()]
        if active_only:
            matches = [g for g in matches if g.phase != "finished"]
        return matches

    def add_game(self, game: Game) -> None:
        self.games[game.game_id] = game
        self.group_to_game[str(game.group_chat_id)] = game.game_id
        self.save()

    def delete_game(self, game: Game) -> None:
        self.games.pop(game.game_id, None)
        self.group_to_game.pop(str(game.group_chat_id), None)
        self.save()


STORE = Store(STATE_FILE)


# ---------------------------------------------------------------------------
# Recovered and repaired bridge engine
# ---------------------------------------------------------------------------

def card_deck() -> list[dict[str, str]]:
    return [{"value": value, "suit": suit} for value in VALUES_ASC for suit in CARD_SUITS]


def value_number(value: str) -> int:
    return VALUES_ASC.index(value) + 2


def bid_label(bid_number: int) -> str:
    level = bid_number // 5 + 1
    suit = BID_SUITS[bid_number % 5]
    return f"{level} {suit}"


def bid_level(bid_number: int) -> int:
    return bid_number // 5 + 1


def bid_suit(bid_number: int) -> str:
    return BID_SUITS[bid_number % 5]


def hand_points(cards: Iterable[dict[str, str]]) -> int:
    high_card_points = {"A": 4, "K": 3, "Q": 2, "J": 1}
    counts = {suit: 0 for suit in CARD_SUITS}
    points = 0
    for card in cards:
        counts[card["suit"]] += 1
        points += high_card_points.get(card["value"], 0)
    points += sum(max(0, count - 4) for count in counts.values())
    return points


def deal_hands() -> list[dict[str, list[str]]]:
    """Deal four sorted hands; redeal while any hand is too weak for play."""
    while True:
        deck = card_deck()
        _rng.shuffle(deck)
        raw_hands = [deck[start:start + 13] for start in range(0, 52, 13)]
        if all(hand_points(raw) > WASH_IF_POINTS_AT_MOST for raw in raw_hands):
            break

    hands: list[dict[str, list[str]]] = []
    for raw in raw_hands:
        hand = {suit: [] for suit in CARD_SUITS}
        for card in raw:
            hand[card["suit"]].append(card["value"])
        for suit in CARD_SUITS:
            hand[suit].sort(key=value_number, reverse=True)
        hands.append(hand)
    return hands


def hand_text(hand: dict[str, list[str]]) -> str:
    lines = []
    for suit in CARD_SUITS:
        values = ", ".join(hand[suit]) if hand[suit] else "🚫"
        lines.append(f"{suit}  -  {values}")
    return "\n".join(lines)


def ordered_hand_cards(hand: dict[str, list[str]]) -> list[tuple[str, str]]:
    return [(suit, value) for suit in CARD_SUITS for value in hand.get(suit, [])]


def numbered_hand_text(hand: dict[str, list[str]]) -> str:
    cards = ordered_hand_cards(hand)
    if not cards:
        return "Card-number map: no cards left."
    lines = ["Card-number map for group buttons"]
    lines.extend(f"{index:02d}. {value} {suit}" for index, (suit, value) in enumerate(cards, start=1))
    return "\n".join(lines)


def card_from_numbered_slot(hand: dict[str, list[str]], slot: int) -> tuple[str, str]:
    cards = ordered_hand_cards(hand)
    if slot < 1 or slot > len(cards):
        raise ValueError("That numbered card slot is absent from your current hand.")
    return cards[slot - 1]


def hand_contains(hand: dict[str, list[str]], suit: str, value: str) -> bool:
    return value in hand.get(suit, [])


def total_cards(hand: dict[str, list[str]]) -> int:
    return sum(len(cards) for cards in hand.values())


def legal_suits(
    hand: dict[str, list[str]],
    trump_suit: Optional[str],
    led_suit: Optional[str],
    trump_broken: bool,
) -> list[str]:
    if led_suit:
        if hand[led_suit]:
            return [led_suit]
        return [suit for suit in CARD_SUITS if hand[suit]]

    nonempty = [suit for suit in CARD_SUITS if hand[suit]]
    if trump_suit is None or trump_broken:
        return nonempty
    non_trumps = [suit for suit in nonempty if suit != trump_suit]
    return non_trumps or [trump_suit]


def winning_card_index(cards: list[PlayedCard], led_suit: str, trump_suit: Optional[str]) -> int:
    best_index = 0
    for index in range(1, len(cards)):
        challenger = cards[index]
        incumbent = cards[best_index]
        challenger_trump = trump_suit is not None and challenger.suit == trump_suit
        incumbent_trump = trump_suit is not None and incumbent.suit == trump_suit
        challenger_wins = (
            (challenger_trump and not incumbent_trump)
            or (
                challenger.suit == led_suit
                and incumbent.suit != led_suit
                and not incumbent_trump
            )
            or (
                challenger.suit == incumbent.suit
                and value_number(challenger.value) > value_number(incumbent.value)
            )
        )
        if challenger_wins:
            best_index = index
    return best_index


def new_game_id() -> str:
    """Return a compact, Telegram-safe table code with a little kopi-shop voltage."""
    while True:
        prefix = _rng.choice(GAME_CODE_PREFIXES)
        noun = _rng.choice(GAME_CODE_NOUNS)
        digit = _rng.randrange(10)
        game_id = f"{prefix}{noun}{digit}"
        if len(game_id) <= 24 and game_id not in STORE.games:
            return game_id


def legacy_random_game_id() -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        game_id = "".join(secrets.choice(alphabet) for _ in range(7))
        if game_id not in STORE.games:
            return game_id


def begin_deal(game: Game, rotate_dealer: bool = False) -> None:
    if len(game.players) != PLAYERS:
        raise ValueError("A deal requires exactly four players.")
    if rotate_dealer:
        game.dealer_pos = (game.dealer_pos + 1) % PLAYERS
    dealt = deal_hands()
    game.deal_number += 1
    game.hands = {str(player.user_id): dealt[pos] for pos, player in enumerate(game.players)}
    game.phase = "bidding"
    game.turn_pos = game.dealer_pos
    game.bid_history = []
    game.highest_bid = None
    game.highest_bidder_id = None
    game.passes_in_row = 0
    game.contract_bid = None
    game.declarer_id = None
    game.trump_suit = None
    game.called_card = None
    game.partner_id = None
    game.partner_revealed = False
    game.trick_number = 0
    game.current_trick = []
    game.trump_broken = False
    game.tricks_won = {str(player.user_id): 0 for player in game.players}
    game.result_text = None
    for player in game.players:
        game.score.setdefault(str(player.user_id), 0)


def record_bid(game: Game, user_id: int, chosen_bid: Optional[int]) -> str:
    if game.phase != "bidding":
        raise ValueError("Bidding is closed.")
    if game.current_player().user_id != user_id:
        raise ValueError("It is another player's turn.")

    if chosen_bid is None:
        game.bid_history.append({"user_id": user_id, "bid": None})
        game.passes_in_row += 1
        action = "Pass"
    else:
        if chosen_bid < 0 or chosen_bid > 34:
            raise ValueError("Invalid bid.")
        if game.highest_bid is not None and chosen_bid <= game.highest_bid:
            raise ValueError("A new bid must be higher than the current bid.")
        game.highest_bid = chosen_bid
        game.highest_bidder_id = user_id
        game.passes_in_row = 0
        game.bid_history.append({"user_id": user_id, "bid": chosen_bid})
        action = bid_label(chosen_bid)

    if game.highest_bid is None and game.passes_in_row >= PLAYERS:
        begin_deal(game, rotate_dealer=True)
        return "all_pass_redeal"

    if game.highest_bid is not None and game.passes_in_row >= 3:
        game.phase = "calling"
        game.contract_bid = game.highest_bid
        game.declarer_id = game.highest_bidder_id
        game.trump_suit = None if bid_suit(game.contract_bid) == "🚫" else bid_suit(game.contract_bid)
        return "auction_complete"

    game.turn_pos = (game.turn_pos + 1) % PLAYERS
    return action


def choose_called_card(game: Game, user_id: int, suit: str, value: str) -> None:
    if game.phase != "calling" or game.declarer_id != user_id:
        raise ValueError("Only the declarer may call the partner card now.")
    if suit not in CARD_SUITS or value not in VALUES_ASC:
        raise ValueError("Invalid partner card.")
    game.called_card = f"{value} {suit}"
    for player in game.players:
        if hand_contains(game.hand(player.user_id), suit, value):
            game.partner_id = player.user_id
            break
    if game.partner_id is None:
        raise RuntimeError("Called card owner was not found.")
    game.phase = "playing"
    game.turn_pos = (game.position_of(game.declarer_id) + 1) % PLAYERS


def play_card(game: Game, user_id: int, suit: str, value: str) -> dict[str, Any]:
    if game.phase != "playing":
        raise ValueError("Card play has not begun.")
    if game.current_player().user_id != user_id:
        raise ValueError("It is another player's turn.")
    hand = game.hand(user_id)
    if not hand_contains(hand, suit, value):
        raise ValueError("That card is absent from your hand.")
    led_suit = game.current_trick[0].suit if game.current_trick else None
    if suit not in legal_suits(hand, game.trump_suit, led_suit, game.trump_broken):
        if led_suit and hand[led_suit]:
            raise ValueError(f"You must follow suit: {led_suit}.")
        raise ValueError("Trump has not been broken; lead a non-trump suit.")

    hand[suit].remove(value)
    played = PlayedCard(user_id=user_id, suit=suit, value=value)
    game.current_trick.append(played)
    if game.trump_suit and suit == game.trump_suit:
        game.trump_broken = True
    partner_reveal = False
    if played.label == game.called_card and not game.partner_revealed:
        game.partner_revealed = True
        partner_reveal = True

    if len(game.current_trick) < PLAYERS:
        game.turn_pos = (game.turn_pos + 1) % PLAYERS
        return {"played": played, "trick_complete": False, "partner_reveal": partner_reveal}

    led_suit = game.current_trick[0].suit
    winner_index = winning_card_index(game.current_trick, led_suit, game.trump_suit)
    winner_id = game.current_trick[winner_index].user_id
    completed_cards = list(game.current_trick)
    game.tricks_won[str(winner_id)] += 1
    game.trick_number += 1
    game.current_trick = []
    game.turn_pos = game.position_of(winner_id)
    result: dict[str, Any] = {
        "played": played,
        "trick_complete": True,
        "partner_reveal": partner_reveal,
        "winner_id": winner_id,
        "completed_cards": completed_cards,
        "game_complete": False,
    }
    if game.trick_number >= 13:
        finish_deal(game)
        result["game_complete"] = True
    return result


def finish_deal(game: Game) -> None:
    if game.contract_bid is None or game.declarer_id is None or game.partner_id is None:
        raise RuntimeError("Cannot score an unfinished contract.")
    required = bid_level(game.contract_bid) + 6
    declarer_side = {game.declarer_id, game.partner_id}
    won = sum(game.tricks(player_id) for player_id in declarer_side)
    made = won >= required
    if made:
        points = 2 ** (bid_level(game.contract_bid) - 1)
        winners = declarer_side
        outcome = f"Contract made: {won}/{required} tricks."
    else:
        under = required - won
        points = 2 ** (under - 1)
        winners = set(game.player_ids()) - declarer_side
        outcome = f"Contract defeated by {under}: declarer side took {won}/{required} tricks."
    for player_id in winners:
        game.score[str(player_id)] = game.score.get(str(player_id), 0) + points
    names = ", ".join(game.player_name(player_id) for player_id in winners)
    game.result_text = f"{outcome} {names} score +{points}."
    game.phase = "finished"


# ---------------------------------------------------------------------------
# Simple bot players
# ---------------------------------------------------------------------------

def next_bot_id(game: Game) -> int:
    used = set(game.player_ids())
    candidate = -1000
    while candidate in used:
        candidate -= 1
    return candidate


def hand_strength_from_grouped(hand: dict[str, list[str]]) -> int:
    cards = [
        {"value": value, "suit": suit}
        for suit in CARD_SUITS
        for value in hand.get(suit, [])
    ]
    return hand_points(cards)


def longest_suit(hand: dict[str, list[str]], allowed: Iterable[str] = CARD_SUITS) -> str:
    allowed_list = list(allowed)
    return max(
        allowed_list,
        key=lambda suit: (len(hand.get(suit, [])), max([value_number(v) for v in hand.get(suit, [])] or [0]), CARD_SUITS.index(suit)),
    )


def bot_choose_bid(game: Game, bot_id: int) -> Optional[int]:
    hand = game.hand(bot_id)
    points = hand_strength_from_grouped(hand)
    if points < 12:
        return None

    level = 1 + max(0, min(3, (points - 12) // 4))
    suit_lengths = sorted((len(hand[suit]) for suit in CARD_SUITS), reverse=True)
    balanced = suit_lengths[0] <= 4 and suit_lengths[-1] >= 2
    if balanced and points >= 16:
        suit = "🚫"
    else:
        suit = longest_suit(hand)
    candidate = (level - 1) * 5 + BID_SUITS.index(suit)
    if game.highest_bid is None or candidate > game.highest_bid:
        return candidate
    return None


def bot_choose_called_card(game: Game, bot_id: int) -> tuple[str, str]:
    hand = game.hand(bot_id)
    preferred_suits = []
    if game.trump_suit:
        preferred_suits.append(game.trump_suit)
    preferred_suits.extend([suit for suit in reversed(CARD_SUITS) if suit not in preferred_suits])
    for suit in preferred_suits:
        for value in VALUES_DESC:
            if value not in hand[suit]:
                return suit, value
    raise RuntimeError("Bot could not choose a partner card.")


def card_would_win(game: Game, user_id: int, suit: str, value: str) -> bool:
    if not game.current_trick:
        return False
    hypothetical = list(game.current_trick) + [PlayedCard(user_id=user_id, suit=suit, value=value)]
    winner_index = winning_card_index(hypothetical, game.current_trick[0].suit, game.trump_suit)
    return hypothetical[winner_index].user_id == user_id


def bot_choose_card(game: Game, bot_id: int) -> tuple[str, str]:
    hand = game.hand(bot_id)
    led_suit = game.current_trick[0].suit if game.current_trick else None
    suits = legal_suits(hand, game.trump_suit, led_suit, game.trump_broken)

    if led_suit is None:
        suit = longest_suit(hand, suits)
        # Lead a useful high card rather than burning the absolute lowest card every time.
        return suit, hand[suit][0]

    candidates = [(suit, value) for suit in suits for value in sorted(hand[suit], key=value_number)]
    winning_candidates = [
        (suit, value)
        for suit, value in candidates
        if card_would_win(game, bot_id, suit, value)
    ]
    if winning_candidates:
        return min(winning_candidates, key=lambda item: value_number(item[1]))
    return min(candidates, key=lambda item: value_number(item[1]))


# ---------------------------------------------------------------------------
# Formatting and Telegram keyboards
# ---------------------------------------------------------------------------

def auction_text(game: Game) -> str:
    if not game.bid_history:
        return "Auction: no calls yet."
    calls = []
    for item in game.bid_history:
        call = "Pass" if item["bid"] is None else bid_label(item["bid"])
        calls.append(f"{game.player_name(item['user_id'])}: {call}")
    return "Auction: " + " | ".join(calls)


def contract_text(game: Game) -> str:
    if game.contract_bid is None or game.declarer_id is None:
        return "No contract."
    return f"{game.player_name(game.declarer_id)} declares {bid_label(game.contract_bid)}."


def score_text(game: Game) -> str:
    ranking = sorted(game.players, key=lambda p: game.score.get(str(p.user_id), 0), reverse=True)
    lines = ["Scoreboard"]
    lines.extend(f"{player.name}: {game.score.get(str(player.user_id), 0)}" for player in ranking)
    return "\n".join(lines)


def status_text(game: Game) -> str:
    if game.phase == "lobby":
        seated = ", ".join(p.name for p in game.players) or "none"
        return f"Game {game.game_id}: lobby open. Seated: {seated}."
    if game.phase == "bidding":
        return f"Game {game.game_id}: bidding. {auction_text(game)} Next: {game.current_player().name}."
    if game.phase == "calling":
        return f"Game {game.game_id}: {contract_text(game)} Declarer is choosing a partner card."
    if game.phase == "playing":
        return (
            f"Game {game.game_id}: {contract_text(game)} Trick {game.trick_number + 1}/13. "
            f"Next: {game.current_player().name}.\n\n{trick_counts_text(game)}"
        )
    return f"Game {game.game_id}: finished. {game.result_text}\n\n{score_text(game)}"


def trick_counts_text(game: Game) -> str:
    lines = [f"Tricks taken — trick {min(game.trick_number + 1, 13)}/13"]
    lines.extend(f"{player.name}: {game.tricks(player.user_id)}" for player in game.players)
    return "\n".join(lines)


def current_trick_text(game: Game) -> str:
    if game.phase != "playing":
        return f"Game {game.game_id}: no live trick on the table right now."
    trick_index = game.trick_number + 1
    if not game.current_trick:
        return (
            f"Current trick — game {game.game_id}, trick {trick_index}/13\n"
            "No cards have been played into this trick yet. Freshly empty table. Suspiciously clean."
        )
    lines = [f"Current trick — game {game.game_id}, trick {trick_index}/13"]
    lines.extend(f"{game.player_name(card.user_id)}: {card.label}" for card in game.current_trick)
    lines.append(f"Next: {game.current_player().name}")
    return "\n".join(lines)


def whos_turn_text(game: Game) -> str:
    if game.phase == "lobby":
        return f"Game {game.game_id}: lobby phase. Nobody's turn yet; democracy is still seating itself. 🪑"
    if game.phase == "finished":
        return f"Game {game.game_id}: deal finished. The cards are off-duty. 💤"
    current = game.current_player().name
    name = current.upper()
    emoji = " ".join(_rng.sample(TURN_EMOJIS, k=3))
    if game.phase == "bidding":
        action = "BID"
    elif game.phase == "calling":
        action = "CALL YOUR PARTNER CARD"
    else:
        action = "PLAY YOUR CARD"
    return f"{emoji} {name}, IT'S YOUR TURN TO {action}. {emoji}"


def table_banter_line(game: Game, template_pool: list[str]) -> str:
    current_name = game.current_player().name if game.phase in {"bidding", "calling", "playing"} else "distinguished table"
    return _rng.choice(template_pool).format(name=current_name)


def sorry_undo_text() -> str:
    return _rng.choice(UNDO_LINES)


def human_players(game: Game) -> list[Player]:
    return [player for player in game.players if not player.is_bot]


def hand_status_text(game: Game, user_id: int) -> str:
    hand = game.hand(user_id)
    lines = [f"Your hand — game {game.game_id}", hand_text(hand)]
    if game.phase == "bidding":
        lines.append(auction_text(game))
    elif game.phase in {"calling", "playing", "finished"}:
        lines.append(contract_text(game))
        if game.called_card:
            lines.append(f"Called partner card: {game.called_card}")
        lines.append(trick_counts_text(game))
    return "\n\n".join(lines)


def cb(game: Game, action: str, *args: Any) -> str:
    # Include the deal number so old buttons cannot act on a freshly redealt hand.
    suffix = ":".join(str(arg) for arg in args)
    return f"{game.game_id}:{game.deal_number}:{action}" + (f":{suffix}" if suffix else "")


def bidding_keyboard(game: Game) -> InlineKeyboardMarkup:
    minimum_level = 1 if game.highest_bid is None else bid_level(game.highest_bid)
    level_buttons = [
        InlineKeyboardButton(str(level), callback_data=cb(game, "level", level))
        for level in range(minimum_level, 8)
    ]
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Pass", callback_data=cb(game, "pass"))], level_buttons]
    )


def denomination_keyboard(game: Game, level: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    buttons = []
    for suit_index, suit in enumerate(BID_SUITS):
        bid_num = (level - 1) * 5 + suit_index
        if game.highest_bid is None or bid_num > game.highest_bid:
            buttons.append(InlineKeyboardButton(f"{level} {suit}", callback_data=cb(game, "bid", bid_num)))
    if buttons:
        rows.append(buttons)
    rows.append([InlineKeyboardButton("← Choose another level", callback_data=cb(game, "backbid"))])
    return InlineKeyboardMarkup(rows)


def call_suit_keyboard(game: Game) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(suit, callback_data=cb(game, "callsuit", index)) for index, suit in enumerate(CARD_SUITS)]
    ])


def call_value_keyboard(game: Game, suit_index: int) -> InlineKeyboardMarkup:
    rows = []
    for start in range(0, len(VALUES_DESC), 5):
        rows.append([
            InlineKeyboardButton(value, callback_data=cb(game, "call", suit_index, value))
            for value in VALUES_DESC[start:start + 5]
        ])
    rows.append([InlineKeyboardButton("← Suit", callback_data=cb(game, "callmenu"))])
    return InlineKeyboardMarkup(rows)


def play_keyboard(game: Game, user_id: int) -> InlineKeyboardMarkup:
    # Legacy private inline-keyboard helper kept for compatibility with older saved buttons/tests.
    hand = game.hand(user_id)
    led_suit = game.current_trick[0].suit if game.current_trick else None
    suits = legal_suits(hand, game.trump_suit, led_suit, game.trump_broken)
    rows: list[list[InlineKeyboardButton]] = []
    for suit_index, suit in enumerate(CARD_SUITS):
        if suit not in suits:
            continue
        rows.append([
            InlineKeyboardButton(f"{value} {suit}", callback_data=cb(game, "play", suit_index, value))
            for value in hand[suit]
        ])
    return InlineKeyboardMarkup(rows)


def group_card_number_keyboard(game: Game) -> InlineKeyboardMarkup:
    # Legacy anonymous keypad helper kept so stale buttons can be rejected cleanly.
    rows: list[list[InlineKeyboardButton]] = []
    for start in range(1, 14, 4):
        rows.append([
            InlineKeyboardButton(str(slot), callback_data=cb(game, "playnum", slot))
            for slot in range(start, min(start + 4, 14))
        ])
    return InlineKeyboardMarkup(rows)


def full_hand_card_rows(hand: dict[str, list[str]]) -> list[list[str]]:
    """Return the whole remaining hand as tappable value-first card buttons."""
    rows: list[list[str]] = []
    for suit in CARD_SUITS:
        cards = [f"{value} {suit}" for value in hand[suit]]
        for start in range(0, len(cards), 4):
            rows.append(cards[start:start + 4])
    return rows or [["No cards left"]]


def full_hand_card_keyboard(game: Game, user_id: int) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        full_hand_card_rows(game.hand(user_id)),
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=True,
    )


def reply_card_keyboard(game: Game, user_id: int) -> ReplyKeyboardMarkup:
    # Playing phase: show every remaining card, like holding your real hand.
    # Illegal choices are allowed to appear in chat, then rejected by play_card.
    return full_hand_card_keyboard(game, user_id)


def hand_reference_rows(hand: dict[str, list[str]]) -> list[list[str]]:
    rows: list[list[str]] = []
    for suit in CARD_SUITS:
        values = " ".join(hand[suit]) if hand[suit] else "🚫"
        # Suit-first labels prevent accidental parsing as played cards.
        rows.append([f"{suit} {values}"])
    return rows


def hand_reference_keyboard(game: Game, user_id: int) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        hand_reference_rows(game.hand(user_id)),
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=True,
    )


def player_mention_html(player: Player) -> str:
    return f'<a href="tg://user?id={player.user_id}">{escape(player.name)}</a>'


def parse_card_message(text: str) -> Optional[tuple[str, str]]:
    match = re.fullmatch(r"\s*(10|[2-9JQKAjqka])\s*([♣♦♥♠])\s*", text)
    if not match:
        return None
    return match.group(2), match.group(1).upper()


# ---------------------------------------------------------------------------
# Messaging helpers
# ---------------------------------------------------------------------------

async def group_message(
    context: ContextTypes.DEFAULT_TYPE,
    game: Game,
    text: str,
    reply_markup: Any | None = None,
    parse_mode: str | None = None,
) -> None:
    await context.bot.send_message(
        chat_id=game.group_chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )


async def private_message(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> bool:
    try:
        await context.bot.send_message(chat_id=user_id, text=text, reply_markup=reply_markup)
        return True
    except Forbidden:
        LOGGER.info("Could not private-message user %s; user has not opened bot chat.", user_id)
        return False


async def send_hand_reference_keyboard(context: ContextTypes.DEFAULT_TYPE, game: Game, player: Player) -> None:
    await group_message(
        context,
        game,
        f"🂠 {player_mention_html(player)}",
        hand_reference_keyboard(game, player.user_id),
        parse_mode="HTML",
    )


async def send_full_hand_card_keyboard(context: ContextTypes.DEFAULT_TYPE, game: Game, player: Player) -> None:
    await group_message(
        context,
        game,
        f"🂠 {player_mention_html(player)}",
        full_hand_card_keyboard(game, player.user_id),
        parse_mode="HTML",
    )


async def send_play_phase_reference_keyboards(context: ContextTypes.DEFAULT_TYPE, game: Game) -> None:
    for player in human_players(game):
        await send_full_hand_card_keyboard(context, game, player)


def table_selector_keyboard(games: list[Game], command: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{game.group_title} — {game.game_id}", callback_data=f"select:{command}:{game.game_id}")]
        for game in games
    ])


def private_hand_reply_markup(game: Game, user_id: int) -> InlineKeyboardMarkup | None:
    # Hands remain private references. Live action buttons are posted in the group
    # turn prompt so the player does not need to keep jumping back to this chat.
    return None


async def choose_private_game(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    command: str,
    active_only: bool = False,
) -> Optional[Game]:
    user = update.effective_user
    if user is None:
        return None
    if context.args:
        game_id = context.args[0].upper()
        game = STORE.games.get(game_id)
        if game and user.id in game.player_ids():
            return game
        await update.effective_message.reply_text("That game code is not one of your tables.")
        return None
    games = STORE.games_for_player(user.id, active_only=active_only)
    if not games:
        await update.effective_message.reply_text("No table found.")
        return None
    if len(games) == 1:
        return games[0]
    await update.effective_message.reply_text(
        "Choose which table:",
        reply_markup=table_selector_keyboard(games, command),
    )
    return None


async def prompt_current_bidder(context: ContextTypes.DEFAULT_TYPE, game: Game) -> None:
    player = game.current_player()
    await private_message(context, player.user_id, hand_status_text(game, player.user_id))
    await send_hand_reference_keyboard(context, game, player)
    await group_message(
        context,
        game,
        f"Bidding turn: {player.name}.",
        bidding_keyboard(game),
    )


async def prompt_declarer_to_call(context: ContextTypes.DEFAULT_TYPE, game: Game) -> None:
    if game.declarer_id is None:
        return
    declarer = game.player(game.declarer_id)
    await private_message(context, declarer.user_id, hand_status_text(game, declarer.user_id))
    await send_hand_reference_keyboard(context, game, declarer)
    await group_message(
        context,
        game,
        f"{player_mention_html(declarer)}, choose your partner card.",
        call_suit_keyboard(game),
        parse_mode="HTML",
    )


async def prompt_current_player(context: ContextTypes.DEFAULT_TYPE, game: Game) -> None:
    player = game.current_player()
    led = game.current_trick[0].suit if game.current_trick else None
    suffix = "" if led is None else f" Suit led: {led}."
    await private_message(context, player.user_id, hand_status_text(game, player.user_id))
    await group_message(
        context,
        game,
        f"{player_mention_html(player)}, YOUR TURN.{suffix}",
        reply_card_keyboard(game, player.user_id),
        parse_mode="HTML",
    )


async def notify_trick_counts(context: ContextTypes.DEFAULT_TYPE, game: Game) -> None:
    text = trick_counts_text(game)
    for player in human_players(game):
        await private_message(context, player.user_id, text)


async def announce_trick_result(context: ContextTypes.DEFAULT_TYPE, game: Game, result: dict[str, Any]) -> None:
    cards = " | ".join(
        f"{game.player_name(card.user_id)}: {card.label}" for card in result["completed_cards"]
    )
    await group_message(
        context,
        game,
        f"Trick {game.trick_number}: {cards}\nWinner: {game.player_name(result['winner_id'])}.",
    )
    await notify_trick_counts(context, game)


async def advance_to_human_turn(context: ContextTypes.DEFAULT_TYPE, game: Game) -> None:
    """Let simple bots act until a human must make the next private decision."""
    for _ in range(200):
        if game.phase == "bidding":
            player = game.current_player()
            if not player.is_bot:
                await prompt_current_bidder(context, game)
                return
            chosen = bot_choose_bid(game, player.user_id)
            outcome = record_bid(game, player.user_id, chosen)
            STORE.save()
            call = "Pass" if chosen is None else bid_label(chosen)
            await group_message(context, game, f"{player.name}: {call}")
            if outcome == "all_pass_redeal":
                await group_message(context, game, "All four players passed. The deal has been washed and redealt.")
                for human in human_players(game):
                    await private_message(context, human.user_id, hand_status_text(game, human.user_id))
                continue
            if outcome == "auction_complete":
                await group_message(context, game, f"Auction complete. {contract_text(game)}")
                continue
            continue

        if game.phase == "calling":
            if game.declarer_id is None:
                return
            declarer = game.player(game.declarer_id)
            if not declarer.is_bot:
                await prompt_declarer_to_call(context, game)
                return
            suit, value = bot_choose_called_card(game, declarer.user_id)
            choose_called_card(game, declarer.user_id, suit, value)
            STORE.save()
            await group_message(
                context,
                game,
                f"{declarer.name} calls {game.called_card}.",
            )
            await send_play_phase_reference_keyboards(context, game)
            continue

        if game.phase == "playing":
            player = game.current_player()
            if not player.is_bot:
                await prompt_current_player(context, game)
                return
            suit, value = bot_choose_card(game, player.user_id)
            result = play_card(game, player.user_id, suit, value)
            STORE.save()
            await group_message(context, game, f"{player.name} plays {value} {suit}.")
            if result["partner_reveal"]:
                await group_message(context, game, f"Partner revealed: {game.player_name(game.partner_id)} held {game.called_card}.")
            if result["trick_complete"]:
                await announce_trick_result(context, game, result)
            if result.get("game_complete"):
                await group_message(
                    context,
                    game,
                    f"Deal complete. {game.result_text}\n\n{score_text(game)}\n\nHost may use /next for another deal or /stop to close the table.",
                    reply_markup=ReplyKeyboardRemove(selective=False),
                )
                return
            continue

        return
    await group_message(context, game, "Bot loop safeguard triggered. Use /status and /hand to continue.")


async def deliver_hands_and_begin(context: ContextTypes.DEFAULT_TYPE, game: Game, rotate_dealer: bool = False) -> None:
    begin_deal(game, rotate_dealer=rotate_dealer)
    STORE.save()
    await group_message(
        context,
        game,
        f"Four players seated. Cards dealt for game {game.game_id}. "
        f"Dealer and first bidder: {game.current_player().name}. Hands have been sent privately.",
        reply_markup=ReplyKeyboardRemove(selective=False),
    )
    for player in human_players(game):
        await private_message(context, player.user_id, hand_status_text(game, player.user_id))
    await advance_to_human_turn(context, game)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return

    if chat.type == ChatType.PRIVATE:
        if context.args and context.args[0].startswith("join_"):
            game_id = context.args[0][5:].upper()
            game = STORE.games.get(game_id)
            if game is None or game.phase != "lobby":
                await update.message.reply_text("That lobby has expired or already begun.")
                return
            if user.id in game.player_ids():
                await update.message.reply_text(f"You are already seated in game {game.game_id}.")
                return
            if len(game.players) >= PLAYERS:
                await update.message.reply_text("That table is already full.")
                return
            game.players.append(Player(user_id=user.id, name=user.full_name))
            game.score.setdefault(str(user.id), 0)
            STORE.save()
            seats_left = PLAYERS - len(game.players)
            await update.message.reply_text(f"Seated in {game.group_title}. Game code: {game.game_id}.")
            await group_message(
                context,
                game,
                f"{user.full_name} joined the table. {seats_left} seat(s) remaining."
                if seats_left else f"{user.full_name} joined the table. Table full.",
            )
            if len(game.players) == PLAYERS:
                await deliver_hands_and_begin(context, game)
            return
        await update.message.reply_text(
            "BridgeBot is ready. Create a table from a group using /start, then join through its private Join button."
        )
        return

    existing = STORE.game_for_group(chat.id)
    if existing is not None and existing.phase != "finished":
        await update.message.reply_text(
            f"Game {existing.game_id} is already active. Use /status or /stop."
        )
        return
    if existing is not None:
        STORE.delete_game(existing)

    game = Game(
        game_id=new_game_id(),
        group_chat_id=chat.id,
        group_title=chat.title or "this group",
        host_id=user.id,
    )
    STORE.add_game(game)
    me = await context.bot.get_me()
    join_url = f"https://t.me/{me.username}?start=join_{game.game_id}"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Join privately 🂡", url=join_url)]])
    await update.message.reply_text(
        f"Singapore Bridge table opened by {user.full_name}.\n"
        f"Game code: {game.game_id}\n"
        f"Four players must tap Join privately. Hands stay in DM; turn buttons appear in the group prompt.",
        reply_markup=keyboard,
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return
    if chat.type == ChatType.PRIVATE:
        game = await choose_private_game(update, context, "stop", active_only=False)
        if game is None:
            return
    else:
        game = STORE.game_for_group(chat.id)
    if game is None:
        await update.effective_message.reply_text("No table found.")
        return
    if user.id != game.host_id:
        await update.effective_message.reply_text("Only the table host can close the table.")
        return
    STORE.delete_game(game)
    await group_message(context, game, f"Game {game.game_id} closed by {user.full_name}.")
    if chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text(f"Closed game {game.game_id}.")


async def leave_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None or chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("Use /leave in the bot's private chat before cards are dealt.")
        return
    game = await choose_private_game(update, context, "leave", active_only=True)
    if game is None:
        return
    if game.phase != "lobby":
        await update.effective_message.reply_text("You can leave only while a table is waiting for players.")
        return
    game.players = [player for player in game.players if player.user_id != user.id]
    game.score.pop(str(user.id), None)
    STORE.save()
    await update.effective_message.reply_text(f"You left game {game.game_id}.")
    await group_message(context, game, f"{user.full_name} left the table. {PLAYERS - len(game.players)} seat(s) remaining.")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return
    if chat.type == ChatType.PRIVATE:
        game = await choose_private_game(update, context, "status", active_only=False)
        if game is None:
            return
    else:
        game = STORE.game_for_group(chat.id)
    if game is None:
        await update.effective_message.reply_text("No table found.")
        return
    await update.effective_message.reply_text(status_text(game))


async def hand_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None or chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("Use /hand in the bot's private chat.")
        return
    game = await choose_private_game(update, context, "hand", active_only=False)
    if game is None:
        return
    if game.phase == "lobby":
        await update.message.reply_text("You do not have a dealt hand yet.")
        return
    await update.message.reply_text(
        hand_status_text(game, user.id),
        reply_markup=private_hand_reply_markup(game, user.id),
    )


async def score_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return
    if chat.type == ChatType.PRIVATE:
        game = await choose_private_game(update, context, "score", active_only=False)
        if game is None:
            return
    else:
        game = STORE.game_for_group(chat.id)
    if game is None:
        await update.effective_message.reply_text("No table found.")
        return
    await update.effective_message.reply_text(score_text(game))


async def bots_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None or chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text("Use /bots in the group lobby after at least one human has joined privately.")
        return
    game = STORE.game_for_group(chat.id)
    if game is None:
        await update.effective_message.reply_text("Open a table first with /start.")
        return
    if game.phase != "lobby":
        await update.effective_message.reply_text("Bots can be added only before cards are dealt.")
        return
    if user.id != game.host_id:
        await update.effective_message.reply_text("Only the table host can fill empty seats with bots.")
        return
    if not human_players(game):
        await update.effective_message.reply_text("At least one human should join privately first, so the bot can send that player a hand.")
        return
    seats = PLAYERS - len(game.players)
    if seats <= 0:
        await update.effective_message.reply_text("The table is already full.")
        return
    for index in range(seats):
        game.players.append(Player(user_id=next_bot_id(game), name=f"Bot {index + 1}", is_bot=True))
    STORE.save()
    await group_message(context, game, f"Filled {seats} empty seat(s) with bots. Starting the deal.")
    await deliver_hands_and_begin(context, game)


async def resolve_command_game(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    selector_command: str,
    active_only: bool = True,
) -> Optional[Game]:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return None
    if chat.type == ChatType.PRIVATE:
        return await choose_private_game(update, context, selector_command, active_only=active_only)
    game = STORE.game_for_group(chat.id)
    if game is None:
        await update.effective_message.reply_text("No table found. Open one with /start.")
        return None
    return game


async def send_banter_response(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    game: Game,
    text: str,
    force_group: bool = False,
) -> None:
    chat = update.effective_chat
    if force_group and (chat is None or chat.type == ChatType.PRIVATE):
        await group_message(context, game, text)
        await update.effective_message.reply_text(f"Sent to {game.group_title} — game {game.game_id}.")
        return
    await update.effective_message.reply_text(text)


async def whoseturnah_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    game = await resolve_command_game(update, context, "whoseturnah", active_only=False)
    if game is None:
        return
    await send_banter_response(update, context, game, whos_turn_text(game))


async def currenttrick_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    game = await resolve_command_game(update, context, "currenttrick", active_only=False)
    if game is None:
        return
    await send_banter_response(update, context, game, current_trick_text(game))


async def walao_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    game = await resolve_command_game(update, context, "walao", active_only=True)
    if game is None:
        return
    await send_banter_response(update, context, game, table_banter_line(game, WALAO_LINES), force_group=True)


async def partner_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return
    if chat.type == ChatType.PRIVATE:
        game = await choose_private_game(update, context, "partner", active_only=True)
        if game is None:
            return
    else:
        game = STORE.game_for_group(chat.id)
    if game is None:
        await update.effective_message.reply_text("No active table here.")
        return
    if game.phase != "calling" or game.declarer_id is None:
        await update.effective_message.reply_text("Partner call is not open now.")
        return
    if user.id != game.declarer_id:
        await update.effective_message.reply_text(f"{game.player_name(game.declarer_id)} is choosing partner.")
        return
    await prompt_declarer_to_call(context, game)


async def fasterleh_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    game = await resolve_command_game(update, context, "fasterleh", active_only=True)
    if game is None:
        return
    await send_banter_response(update, context, game, table_banter_line(game, FASTER_LINES), force_group=True)


async def huatah_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    game = await resolve_command_game(update, context, "huatah", active_only=True)
    if game is None:
        return
    await send_banter_response(update, context, game, table_banter_line(game, HUATAH_LINES), force_group=True)


async def sorry_can_i_undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    game = await resolve_command_game(update, context, "sorry_can_i_undo", active_only=False)
    if game is None:
        return
    await send_banter_response(update, context, game, sorry_undo_text(), force_group=True)


async def next_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None or chat.type == ChatType.PRIVATE:
        return
    game = STORE.game_for_group(chat.id)
    if game is None or game.phase != "finished":
        await update.effective_message.reply_text("Finish the current deal before starting the next one.")
        return
    if user.id != game.host_id:
        await update.effective_message.reply_text("Only the table host can start the next deal.")
        return
    await deliver_hands_and_begin(context, game, rotate_dealer=True)


async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Singapore Bridge rules in this bot:\n"
        "• Four players each receive 13 cards privately.\n"
        "• Hands are washed and redealt when any hand has 4 or fewer strength points.\n"
        "• Bids run from 1 ♣ to 7 🚫; three passes after a bid close the auction.\n"
        "• Declarer calls one partner card. Its holder remains hidden until that card is played.\n"
        "• The player left of declarer leads. Follow suit when able; trump cannot be led before it is broken.\n"
        "• Private updates show current trick counts for every player.\n"
        "• Bidding/call turns also show a selective hand-reference keyboard for the current player.\n"
        "• Card play uses the player's selective reply keyboard with every remaining card shown.\n"
        "• Use /currenttrick to show only the cards played into the live trick. No archived trick history.\n"
        "• Use /whoseturnah, /fasterleh, /walao, /huatah, and /sorry_can_i_undo for table theatre.\n"
        "• Use /bots in the group lobby to fill empty seats with simple bot players.\n"
        "• A successful contract scores 1/2/4/8/16/32/64 by level; defeated defenders score by undertricks."
    )


# ---------------------------------------------------------------------------
# Callback actions
# ---------------------------------------------------------------------------

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None or not query.data:
        return
    parts = query.data.split(":")
    if parts[0] == "select":
        if len(parts) != 3:
            await query.answer("Invalid table selector.", show_alert=True)
            return
        command, game_id = parts[1], parts[2]
        game = STORE.games.get(game_id)
        if game is None or user.id not in game.player_ids():
            await query.answer("That table is unavailable.", show_alert=True)
            return
        await query.answer()
        if command == "hand":
            if game.phase == "lobby":
                await query.edit_message_text("You do not have a dealt hand yet.")
            else:
                await query.edit_message_text(
                    hand_status_text(game, user.id),
                    reply_markup=private_hand_reply_markup(game, user.id),
                )
            return
        if command == "status":
            await query.edit_message_text(status_text(game))
            return
        if command == "score":
            await query.edit_message_text(score_text(game))
            return
        if command == "whoseturnah":
            await query.edit_message_text(whos_turn_text(game))
            return
        if command == "currenttrick":
            await query.edit_message_text(current_trick_text(game))
            return
        if command == "walao":
            await group_message(context, game, table_banter_line(game, WALAO_LINES))
            await query.edit_message_text(f"Sent to {game.group_title} — game {game.game_id}.")
            return
        if command == "fasterleh":
            await group_message(context, game, table_banter_line(game, FASTER_LINES))
            await query.edit_message_text(f"Sent to {game.group_title} — game {game.game_id}.")
            return
        if command == "huatah":
            await group_message(context, game, table_banter_line(game, HUATAH_LINES))
            await query.edit_message_text(f"Sent to {game.group_title} — game {game.game_id}.")
            return
        if command == "sorry_can_i_undo":
            await group_message(context, game, sorry_undo_text())
            await query.edit_message_text(f"Sent to {game.group_title} — game {game.game_id}.")
            return
        if command == "leave":
            if game.phase != "lobby":
                await query.edit_message_text("You can leave only while a table is waiting for players.")
                return
            game.players = [player for player in game.players if player.user_id != user.id]
            game.score.pop(str(user.id), None)
            STORE.save()
            await query.edit_message_text(f"You left game {game.game_id}.")
            await group_message(context, game, f"{user.full_name} left the table. {PLAYERS - len(game.players)} seat(s) remaining.")
            return
        if command == "stop":
            if user.id != game.host_id:
                await query.edit_message_text("Only the table host can close the table.")
                return
            STORE.delete_game(game)
            await query.edit_message_text(f"Closed game {game.game_id}.")
            await group_message(context, game, f"Game {game.game_id} closed by {user.full_name}.")
            return
        await query.edit_message_text("Unknown table selector.")
        return
    if len(parts) < 3:
        await query.answer("Invalid button.", show_alert=True)
        return
    game = STORE.games.get(parts[0])
    try:
        button_deal_number = int(parts[1])
    except ValueError:
        await query.answer("Invalid button.", show_alert=True)
        return
    action = parts[2]
    if game is None:
        await query.answer("This game has expired.", show_alert=True)
        await query.edit_message_text("This game has expired.")
        return
    if user.id not in game.player_ids():
        await query.answer("You are not seated at this table.", show_alert=True)
        return
    if button_deal_number != game.deal_number:
        await query.answer("This button belongs to an earlier deal. Use /hand for the current hand.", show_alert=True)
        return

    try:
        if action == "level":
            if game.phase != "bidding" or game.current_player().user_id != user.id:
                raise ValueError("It is no longer your bidding turn.")
            level = int(parts[3])
            await query.answer()
            await query.edit_message_reply_markup(reply_markup=denomination_keyboard(game, level))
            return
        if action == "backbid":
            if game.phase != "bidding" or game.current_player().user_id != user.id:
                raise ValueError("It is no longer your bidding turn.")
            await query.answer()
            await query.edit_message_reply_markup(reply_markup=bidding_keyboard(game))
            return
        if action in {"pass", "bid"}:
            chosen = None if action == "pass" else int(parts[3])
            outcome = record_bid(game, user.id, chosen)
            STORE.save()
            await query.answer()
            call = "Pass" if chosen is None else bid_label(chosen)
            await group_message(
                context,
                game,
                f"{player_mention_html(game.player(user.id))}: {call}",
                reply_markup=ReplyKeyboardRemove(selective=True),
                parse_mode="HTML",
            )
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except BadRequest:
                pass
            if outcome == "all_pass_redeal":
                await group_message(context, game, "All four players passed. The deal has been washed and redealt.")
                for player in human_players(game):
                    await private_message(context, player.user_id, hand_status_text(game, player.user_id))
            elif outcome == "auction_complete":
                await group_message(context, game, f"Auction complete. {contract_text(game)}")
            await advance_to_human_turn(context, game)
            return
        if action == "callmenu":
            if game.phase != "calling" or game.declarer_id != user.id:
                raise ValueError("Partner call is already complete.")
            await query.answer()
            await query.edit_message_reply_markup(reply_markup=call_suit_keyboard(game))
            return
        if action == "callsuit":
            if game.phase != "calling" or game.declarer_id != user.id:
                raise ValueError("Partner call is already complete.")
            suit_index = int(parts[3])
            await query.answer()
            await query.edit_message_reply_markup(reply_markup=call_value_keyboard(game, suit_index))
            return
        if action == "call":
            suit = CARD_SUITS[int(parts[3])]
            value = parts[4]
            choose_called_card(game, user.id, suit, value)
            STORE.save()
            await query.answer()
            await query.edit_message_reply_markup(reply_markup=None)
            await group_message(
                context,
                game,
                f"{player_mention_html(game.player(user.id))} calls {game.called_card}.",
                reply_markup=ReplyKeyboardRemove(selective=True),
                parse_mode="HTML",
            )
            await send_play_phase_reference_keyboards(context, game)
            await advance_to_human_turn(context, game)
            return
        if action in {"play", "playnum"}:
            if action == "playnum":
                slot = int(parts[3])
                suit, value = card_from_numbered_slot(game.hand(user.id), slot)
            else:
                suit = CARD_SUITS[int(parts[3])]
                value = parts[4]
            result = play_card(game, user.id, suit, value)
            STORE.save()
            await query.answer()
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except BadRequest:
                pass
            await private_message(context, user.id, hand_status_text(game, user.id))
            await group_message(
                context,
                game,
                f"✅ {player_mention_html(game.player(user.id))} plays {value} {suit}.",
                reply_markup=ReplyKeyboardRemove(selective=False) if result.get("game_complete") else full_hand_card_keyboard(game, user.id),
                parse_mode="HTML",
            )
            if result["partner_reveal"]:
                await group_message(context, game, f"Partner revealed: {game.player_name(game.partner_id)} held {game.called_card}.")
            if result["trick_complete"]:
                await announce_trick_result(context, game, result)
            if result.get("game_complete"):
                await group_message(
                    context,
                    game,
                    f"Deal complete. {game.result_text}\n\n{score_text(game)}\n\nHost may use /next for another deal or /stop to close the table.",
                    reply_markup=ReplyKeyboardRemove(selective=False),
                )
            else:
                await advance_to_human_turn(context, game)
            return
        await query.answer("Unknown button action.", show_alert=True)
    except (IndexError, ValueError) as exc:
        await query.answer(str(exc), show_alert=True)
    except TelegramError as exc:
        LOGGER.exception("Telegram callback failure: %s", exc)
        await query.answer("Telegram rejected that action; use /hand to refresh.", show_alert=True)


async def group_card_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat is None or user is None or message is None or message.text is None:
        return
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    game = STORE.game_for_group(chat.id)
    if game is None or game.phase != "playing":
        return
    parsed = parse_card_message(message.text)
    if parsed is None:
        return
    if user.id not in game.player_ids():
        return
    current = game.current_player()
    if current.user_id != user.id:
        await message.reply_text(f"{current.name}'s turn.")
        return

    suit, value = parsed
    try:
        result = play_card(game, user.id, suit, value)
    except ValueError as exc:
        await message.reply_text(f"Illegal move: {exc}")
        await send_full_hand_card_keyboard(context, game, game.player(user.id))
        return

    STORE.save()
    await group_message(
        context,
        game,
        f"✅ {player_mention_html(current)} plays {value} {suit}.",
        reply_markup=ReplyKeyboardRemove(selective=False) if result.get("game_complete") else full_hand_card_keyboard(game, user.id),
        parse_mode="HTML",
    )
    await private_message(context, user.id, hand_status_text(game, user.id))
    if result["partner_reveal"]:
        await group_message(context, game, f"Partner revealed: {game.player_name(game.partner_id)} held {game.called_card}.")
    if result["trick_complete"]:
        await announce_trick_result(context, game, result)
    if result.get("game_complete"):
        await group_message(
            context,
            game,
            f"Deal complete. {game.result_text}\n\n{score_text(game)}\n\nHost may use /next for another deal or /stop to close the table.",
            reply_markup=ReplyKeyboardRemove(selective=False),
        )
    else:
        await advance_to_human_turn(context, game)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.exception("Unhandled update failure", exc_info=context.error)


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("start", "Open or join a bridge table"),
        BotCommand("status", "Show current table status"),
        BotCommand("hand", "Show your private hand"),
        BotCommand("score", "Show session scores"),
        BotCommand("bots", "Fill empty seats with bot players"),
        BotCommand("partner", "Redraw partner-call buttons"),
        BotCommand("whoseturnah", "Scream whose turn it is"),
        BotCommand("currenttrick", "Show only cards in the current trick"),
        BotCommand("fasterleh", "Politely bully the current player"),
        BotCommand("walao", "Issue a table complaint"),
        BotCommand("huatah", "Bless the table with confidence"),
        BotCommand("sorry_can_i_undo", "Ask for undo and receive reality"),
        BotCommand("next", "Start the next deal (host)"),
        BotCommand("leave", "Leave an open lobby"),
        BotCommand("stop", "Close the table (host)"),
        BotCommand("rules", "Show the rules used"),
    ])


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def build_application(token: str) -> Application:
    application = ApplicationBuilder().token(token).concurrent_updates(False).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("newgame", start_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("leave", leave_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("hand", hand_command))
    application.add_handler(CommandHandler("score", score_command))
    application.add_handler(CommandHandler("bots", bots_command))
    application.add_handler(CommandHandler("fillbots", bots_command))
    application.add_handler(CommandHandler("partner", partner_command))
    application.add_handler(CommandHandler("call", partner_command))
    application.add_handler(CommandHandler("choosepartner", partner_command))
    application.add_handler(CommandHandler("whoseturnah", whoseturnah_command))
    application.add_handler(CommandHandler("whose_turn_ah", whoseturnah_command))
    application.add_handler(CommandHandler("currenttrick", currenttrick_command))
    application.add_handler(CommandHandler("roundcards", currenttrick_command))
    application.add_handler(CommandHandler("tablecards", currenttrick_command))
    application.add_handler(CommandHandler("fasterleh", fasterleh_command))
    application.add_handler(CommandHandler("walao", walao_command))
    application.add_handler(CommandHandler("huatah", huatah_command))
    application.add_handler(CommandHandler("sorry_can_i_undo", sorry_can_i_undo_command))
    application.add_handler(CommandHandler("undo", sorry_can_i_undo_command))
    application.add_handler(CommandHandler("next", next_command))
    application.add_handler(CommandHandler("rules", rules_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, group_card_text_handler))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_error_handler(error_handler)
    return application


def _clean_webhook_path(raw_path: str) -> str:
    """Return a Telegram-webhook path without surrounding slashes."""
    path = (raw_path or "telegram").strip().strip("/")
    return path or "telegram"


def _should_run_webhook() -> bool:
    mode = os.getenv("BRIDGEBOT_MODE", "").strip().lower()
    if mode in {"webhook", "render"}:
        return True
    if mode in {"polling", "local"}:
        return False
    # Render exposes PORT for web services. WEBHOOK_URL is the public onrender.com
    # base URL that you set in the dashboard, e.g. https://bridgebot.onrender.com.
    return bool(os.getenv("PORT") and (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")))


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
    )
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN. In PowerShell: $env:TELEGRAM_BOT_TOKEN='paste-token-here'"
        )

    application = build_application(token)
    LOGGER.info("Starting BridgeBot; state file: %s", STATE_FILE.resolve())

    if _should_run_webhook():
        base_url = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
        if not base_url:
            raise SystemExit("Webhook mode needs WEBHOOK_URL, e.g. https://your-service.onrender.com")

        port = int(os.getenv("PORT", "10000"))
        path = _clean_webhook_path(os.getenv("WEBHOOK_PATH", "telegram"))
        webhook_url = f"{base_url}/{path}"
        secret_token = os.getenv("WEBHOOK_SECRET_TOKEN") or None

        LOGGER.info("Running BridgeBot in webhook mode on 0.0.0.0:%s/%s", port, path)
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=path,
            webhook_url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
            secret_token=secret_token,
        )
    else:
        LOGGER.info("Running BridgeBot in local polling mode")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
