import unittest

from bridgebot_fixed import (
    CARD_SUITS,
    Game,
    PlayedCard,
    Player,
    WASH_IF_POINTS_AT_MOST,
    begin_deal,
    bid_label,
    choose_called_card,
    deal_hands,
    hand_points,
    legal_suits,
    play_card,
    record_bid,
    winning_card_index,
    bot_choose_bid,
    bot_choose_called_card,
    bot_choose_card,
    cb,
    next_bot_id,
    trick_counts_text,
    current_trick_text,
    whos_turn_text,
    sorry_undo_text,
    new_game_id,
    ordered_hand_cards,
    numbered_hand_text,
    card_from_numbered_slot,
    reply_card_keyboard,
    hand_reference_keyboard,
    hand_reference_rows,
    parse_card_message,
)


class EngineTests(unittest.TestCase):
    def make_game(self):
        return Game(
            game_id="TEST001",
            group_chat_id=1,
            group_title="Test",
            host_id=1,
            players=[Player(i, f"P{i}") for i in range(1, 5)],
        )

    def test_deal_contains_52_unique_cards_and_obeys_wash_rule(self):
        hands = deal_hands()
        cards = []
        for hand in hands:
            raw = []
            self.assertEqual(sum(len(hand[s]) for s in CARD_SUITS), 13)
            for suit in CARD_SUITS:
                raw += [{"value": v, "suit": suit} for v in hand[suit]]
                cards += [(v, suit) for v in hand[suit]]
            self.assertGreater(hand_points(raw), WASH_IF_POINTS_AT_MOST)
        self.assertEqual(len(cards), 52)
        self.assertEqual(len(set(cards)), 52)

    def test_bid_labels(self):
        self.assertEqual(bid_label(0), "1 ♣")
        self.assertEqual(bid_label(4), "1 🚫")
        self.assertEqual(bid_label(34), "7 🚫")

    def test_auction_closes_after_bid_and_three_passes(self):
        game = self.make_game()
        begin_deal(game)
        self.assertEqual(record_bid(game, 1, 7), "2 ♥")
        self.assertEqual(record_bid(game, 2, None), "Pass")
        self.assertEqual(record_bid(game, 3, None), "Pass")
        self.assertEqual(record_bid(game, 4, None), "auction_complete")
        self.assertEqual(game.phase, "calling")
        self.assertEqual(game.declarer_id, 1)
        self.assertEqual(game.trump_suit, "♥")

    def test_card_winner_with_trump(self):
        cards = [
            PlayedCard(1, "A", "♣"),
            PlayedCard(2, "2", "♥"),
            PlayedCard(3, "K", "♣"),
            PlayedCard(4, "A", "♦"),
        ]
        self.assertEqual(winning_card_index(cards, "♣", "♥"), 1)

    def test_follow_suit_and_leading_unbroken_trump(self):
        hand = {"♣": ["A"], "♦": [], "♥": ["2"], "♠": []}
        self.assertEqual(legal_suits(hand, "♥", None, False), ["♣"])
        self.assertEqual(legal_suits(hand, "♥", "♣", False), ["♣"])
        self.assertEqual(legal_suits(hand, "♥", "♦", False), ["♣", "♥"])

    def test_called_card_sets_hidden_partner(self):
        game = self.make_game()
        begin_deal(game)
        game.phase = "calling"
        game.declarer_id = 1
        # Select a real card held by player 3.
        suit = next(s for s in CARD_SUITS if game.hand(3)[s])
        value = game.hand(3)[suit][0]
        choose_called_card(game, 1, suit, value)
        self.assertEqual(game.phase, "playing")
        self.assertEqual(game.partner_id, 3)
        self.assertFalse(game.partner_revealed)

    def test_complete_simulated_deal_and_scoring(self):
        game = self.make_game()
        begin_deal(game)
        record_bid(game, 1, 0)
        record_bid(game, 2, None)
        record_bid(game, 3, None)
        record_bid(game, 4, None)
        suit = next(s for s in CARD_SUITS if game.hand(2)[s])
        value = game.hand(2)[suit][0]
        choose_called_card(game, 1, suit, value)
        while game.phase == "playing":
            current = game.current_player().user_id
            hand = game.hand(current)
            led = game.current_trick[0].suit if game.current_trick else None
            suits = legal_suits(hand, game.trump_suit, led, game.trump_broken)
            selected_suit = suits[0]
            selected_value = hand[selected_suit][0]
            play_card(game, current, selected_suit, selected_value)
        self.assertEqual(game.phase, "finished")
        self.assertEqual(game.trick_number, 13)
        self.assertEqual(sum(game.tricks_won.values()), 13)
        self.assertGreater(sum(game.score.values()), 0)

    def test_button_payload_changes_on_redeal(self):
        game = self.make_game()
        begin_deal(game)
        first = cb(game, "pass")
        begin_deal(game, rotate_dealer=True)
        second = cb(game, "pass")
        self.assertNotEqual(first, second)
        self.assertIn(":1:pass", first)
        self.assertIn(":2:pass", second)

    def test_same_user_can_exist_in_two_tables(self):
        first = Game("TABLE01", 10, "A", 1, players=[Player(99, "Human")])
        second = Game("TABLE02", 20, "B", 2, players=[Player(99, "Human")])
        self.assertIn(99, first.player_ids())
        self.assertIn(99, second.player_ids())

    def test_trick_counts_show_all_players(self):
        game = self.make_game()
        begin_deal(game)
        game.phase = "playing"
        game.tricks_won = {"1": 2, "2": 1, "3": 0, "4": 3}
        text = trick_counts_text(game)
        self.assertIn("P1: 2", text)
        self.assertIn("P4: 3", text)

    def test_bot_helpers_choose_legal_actions(self):
        game = self.make_game()
        game.players[0].is_bot = True
        begin_deal(game)
        bot_id = game.players[0].user_id
        bid = bot_choose_bid(game, bot_id)
        self.assertTrue(bid is None or 0 <= bid <= 34)
        game.phase = "calling"
        game.declarer_id = bot_id
        suit, value = bot_choose_called_card(game, bot_id)
        self.assertNotIn(value, game.hand(bot_id)[suit])
        game.phase = "playing"
        game.turn_pos = 0
        suit, value = bot_choose_card(game, bot_id)
        self.assertIn(value, game.hand(bot_id)[suit])
        self.assertLess(next_bot_id(game), 0)

    def test_current_trick_text_shows_only_live_cards(self):
        game = self.make_game()
        begin_deal(game)
        game.phase = "playing"
        game.current_trick = [PlayedCard(1, "A", "♣"), PlayedCard(2, "2", "♣")]
        game.turn_pos = 2
        text = current_trick_text(game)
        self.assertIn("Current trick", text)
        self.assertIn("P1: A ♣", text)
        self.assertIn("P2: 2 ♣", text)
        self.assertIn("Next: P3", text)
        self.assertNotIn("Trick 1:", text)

    def test_fun_turn_and_undo_texts_are_stateless(self):
        game = self.make_game()
        begin_deal(game)
        text = whos_turn_text(game)
        self.assertIn("P1", text)
        self.assertIn("TURN", text)
        undo = sorry_undo_text()
        self.assertTrue(undo)
        self.assertEqual(game.phase, "bidding")

    def test_funny_game_code_is_telegram_safe(self):
        code = new_game_id()
        self.assertTrue(code.isupper())
        self.assertLessEqual(len(code), 24)
        self.assertRegex(code, r"^[A-Z0-9_\-]+$")

    def test_numbered_hand_map_matches_private_reference(self):
        hand = {"♣": ["A", "10"], "♦": ["K"], "♥": [], "♠": ["2"]}
        self.assertEqual(ordered_hand_cards(hand), [("♣", "A"), ("♣", "10"), ("♦", "K"), ("♠", "2")])
        self.assertEqual(card_from_numbered_slot(hand, 3), ("♦", "K"))
        text = numbered_hand_text(hand)
        self.assertIn("01. A ♣", text)
        self.assertIn("03. K ♦", text)
        with self.assertRaises(ValueError):
            card_from_numbered_slot(hand, 5)

    def test_group_reply_keyboard_contains_every_remaining_card_as_buttons(self):
        game = self.make_game()
        begin_deal(game)
        game.phase = "playing"
        game.turn_pos = 0
        keyboard = reply_card_keyboard(game, 1)
        labels = [button.text for row in keyboard.keyboard for button in row]
        expected_labels = [f"{value} {suit}" for suit, value in ordered_hand_cards(game.hand(1))]
        self.assertEqual(labels, expected_labels)
        self.assertTrue(all(parse_card_message(label) is not None for label in labels))
        self.assertTrue(keyboard.selective)
        self.assertFalse(keyboard.one_time_keyboard)


    def test_bidding_hand_reference_keyboard_is_suit_first_and_selective(self):
        game = self.make_game()
        begin_deal(game)
        keyboard = hand_reference_keyboard(game, 1)
        labels = [button.text for row in keyboard.keyboard for button in row]
        self.assertEqual(len(labels), 4)
        self.assertTrue(all(label[0] in CARD_SUITS for label in labels))
        self.assertTrue(keyboard.selective)
        self.assertTrue(all(parse_card_message(label) is None for label in labels))

    def test_card_message_parser_accepts_compact_and_spaced_cards(self):
        self.assertEqual(parse_card_message("A ♠"), ("♠", "A"))
        self.assertEqual(parse_card_message("10♣"), ("♣", "10"))
        self.assertEqual(parse_card_message("q ♦"), ("♦", "Q"))
        self.assertIsNone(parse_card_message("hello table"))


if __name__ == "__main__":
    unittest.main()
