import asyncio
import unittest
from pathlib import Path
import tempfile
import json
from unittest.mock import AsyncMock, MagicMock, patch

from core.knowledge_base import KnowledgeBase
from handlers.check_handler import CheckHandler
from core.gemini_solver import GeminiSolver
from core.notifier import EmergencyNotifier

class TestAutoHexaComponents(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self.temp_path = Path(self.temp_file.name)
        self.temp_file.close()
        initial_data = {
            "how many moves can a pokemon know at the same time": "4",
            "which ball has 100% catch rate": "Master Ball"
        }
        with open(self.temp_path, "w", encoding="utf-8") as f:
            json.dump(initial_data, f)
        self.kb = KnowledgeBase(self.temp_path)

    async def asyncTearDown(self):
        if self.temp_path.exists():
            self.temp_path.unlink()

    async def test_knowledge_base_exact_and_fuzzy(self):
        # Exact match
        ans = await self.kb.find_answer("How many moves can a Pokemon know at the same time?")
        self.assertEqual(ans, "4")

        # Case-insensitive & punctuation
        ans2 = await self.kb.find_answer("HOW MANY MOVES CAN A POKEMON KNOW AT THE SAME TIME???")
        self.assertEqual(ans2, "4")

        # Substring / key match
        ans3 = await self.kb.find_answer("Which ball has 100% catch rate in battles?")
        self.assertEqual(ans3, "Master Ball")

    async def test_knowledge_base_save_and_reload(self):
        await self.kb.save_answer("What type is Pikachu?", "Electric")
        ans = await self.kb.find_answer("What type is Pikachu?")
        self.assertEqual(ans, "Electric")

        # Reload from disk
        reloaded_kb = KnowledgeBase(self.temp_path)
        ans_reloaded = await reloaded_kb.find_answer("What type is Pikachu?")
        self.assertEqual(ans_reloaded, "Electric")

    async def test_check_handler_detection_and_extraction(self):
        notifier = EmergencyNotifier(user_id=123)
        solver = GeminiSolver(api_key="fake")
        handler = CheckHandler(self.kb, solver, notifier)

        # Mock message
        mock_msg = MagicMock()
        mock_msg.raw_text = "Just a quick check.\nHow many moves can a Pokemon know at the same time?\nAnswer within 30s."
        
        self.assertTrue(handler.is_check_message(mock_msg))
        clean_q = handler.extract_question(mock_msg.raw_text)
        self.assertIn("How many moves can a Pokemon know at the same time?", clean_q)
        self.assertNotIn("Just a quick check", clean_q)

    @patch("aiohttp.ClientSession.post")
    async def test_gemini_solver_mock(self, mock_post):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": '{"answer": "4", "confidence": 1.0, "explanation": "A Pokémon can only know up to 4 moves simultaneously."}'}
                        ]
                    }
                }
            ]
        })
        mock_post.return_value.__aenter__.return_value = mock_resp

        solver = GeminiSolver(api_key="test_key", model="gemini-2.5-flash")
        ans, conf, reason = await solver.solve("How many moves can a Pokemon know at the same time?", ["2", "4", "6"])
        self.assertEqual(ans, "4")
        self.assertEqual(conf, 1.0)
        self.assertIn("4 moves", reason)

    async def test_spawn_handler_and_ball_selection(self):
        from handlers.spawn_handler import SpawnHandler
        spawn_handler = SpawnHandler()

        mock_msg = MagicMock()
        mock_msg.raw_text = "A wild Charizard appeared! Level 45."
        self.assertTrue(spawn_handler.is_spawn_message(mock_msg))
        self.assertEqual(spawn_handler.extract_pokemon_name(mock_msg.raw_text), "Charizard")

        # Mock buttons
        btn_poke = MagicMock()
        btn_poke.text = "Pokeball (x15)"
        btn_poke.click = AsyncMock()
        btn_ultra = MagicMock()
        btn_ultra.text = "Ultraball (x5)"
        btn_ultra.click = AsyncMock()
        btn_run = MagicMock()
        btn_run.text = "Run"
        btn_run.click = AsyncMock()
        mock_msg.buttons = [[btn_poke, btn_ultra], [btn_run]]

        mock_client = AsyncMock()
        await spawn_handler.handle_spawn(mock_client, "test_account", mock_msg)
        # Should click preferred Ultraball over Pokeball
        btn_ultra.click.assert_called_once()
        btn_poke.click.assert_not_called()

        # Catch result stats
        result_msg = MagicMock()
        result_msg.raw_text = "Congratulations! You caught Charizard!"
        self.assertTrue(spawn_handler.is_catch_result(result_msg))
        spawn_handler.handle_catch_result("test_account", result_msg)
        self.assertEqual(spawn_handler.stats["caught"], 1)

    def test_auto_hunter_cooldown_parsing(self):
        from core.auto_hunter import AutoHunter
        from handlers.check_handler import CheckHandler
        mock_ch = MagicMock(spec=CheckHandler)
        mock_ch.paused_accounts = set()
        hunter = AutoHunter(mock_ch)

        cd = hunter.extract_cooldown("Please wait 8.5 seconds before hunting again.")
        self.assertEqual(cd, 8.5)

        cd2 = hunter.extract_cooldown("You can hunt again in 12s.")
        self.assertEqual(cd2, 12.0)

        cd3 = hunter.extract_cooldown("No cooldown mention here.")
        self.assertIsNone(cd3)

    async def test_battle_kill_mode_for_pd(self):
        import config
        from core.pokedex import PokedexService
        from handlers.battle_handler import BattleHandler

        pokedex = PokedexService(config.POKEDEX_FILE)
        battle_handler = BattleHandler(pokedex)
        config.BATTLE_SYSTEM = "kill"

        mock_msg = MagicMock()
        mock_msg.raw_text = (
            "Battle begins\n\n"
            "Wild Metapod [Bug]\n"
            "Lv. 8 • HP 27/27\n"
            "Lower Metapod's HP for a better chance of catching it\n\n"
            "Current turn: Shivam\n"
            "Charmander [Fire]\n"
            "Lv. 5 • HP 19/19\n\n"
            "Scratch [Normal]\n"
            "Power: 40, Accuracy: 100\n"
            "Ember [Fire]\n"
            "Power: 40, Accuracy: 100"
        )

        btn_scratch = MagicMock(text="Scratch", click=AsyncMock())
        btn_ember = MagicMock(text="Ember", click=AsyncMock())
        btn_ball = MagicMock(text="Poke Ball", click=AsyncMock())
        btn_run = MagicMock(text="Run", click=AsyncMock())
        mock_msg.buttons = [[btn_scratch, btn_ember], [btn_ball, btn_run]]

        mock_client = AsyncMock()
        await battle_handler.handle_battle_turn(mock_client, "test_account", mock_msg)

        # In Kill Mode, Ember (super-effective Fire vs Bug) must be clicked!
        btn_ember.click.assert_called_once()
        btn_scratch.click.assert_not_called()
        btn_ball.click.assert_not_called()

        # Test battle victory with PD reward
        win_msg = MagicMock()
        win_msg.raw_text = "Wild Metapod fainted! You gained 180 exp and 350 PD!"
        self.assertTrue(battle_handler.is_battle_end(win_msg))
        battle_handler.handle_battle_result("test_account", win_msg)
        self.assertEqual(battle_handler.stats["pd_earned"], 350)
        self.assertEqual(battle_handler.stats["battles_won"], 1)

    async def test_battle_hybrid_mode_with_star_repeat_ball(self):
        import config
        from core.pokedex import PokedexService
        from handlers.battle_handler import BattleHandler

        pokedex = PokedexService(config.POKEDEX_FILE)
        battle_handler = BattleHandler(pokedex)
        config.BATTLE_SYSTEM = "hybrid"

        # Case 1: Pokémon has '☆' -> MUST THROW REPEAT BALL
        mock_msg = MagicMock()
        mock_msg.raw_text = (
            "Battle begins\n\n"
            "Wild Metapod ☆ [Bug]\n"
            "Lv. 8 • HP 27/27\n\n"
            "Current turn: Shivam\n"
            "Charmander [Fire]"
        )

        btn_ember = MagicMock(text="Ember", click=AsyncMock())
        btn_repeat = MagicMock(text="Repeat Ball", click=AsyncMock())
        btn_poke = MagicMock(text="Poke Ball", click=AsyncMock())
        mock_msg.buttons = [[btn_ember], [btn_repeat, btn_poke]]

        mock_client = AsyncMock()
        await battle_handler.handle_battle_turn(mock_client, "test_account", mock_msg)

        # Must choose Repeat Ball because of '☆'!
        btn_repeat.click.assert_called_once()
        btn_poke.click.assert_not_called()
        btn_ember.click.assert_not_called()

    def test_auto_hunter_encounter_lock(self):
        from core.auto_hunter import AutoHunter
        from handlers.check_handler import CheckHandler
        mock_ch = MagicMock(spec=CheckHandler)
        mock_ch.paused_accounts = set()
        hunter = AutoHunter(mock_ch)

        event = hunter.get_encounter_event("test_acc")
        self.assertTrue(event.is_set())

        # When hunt starts, lock is cleared
        event.clear()
        self.assertFalse(event.is_set())

        # When battle ends or catch completes, lock is released
        hunter.mark_encounter_complete("test_acc")
        self.assertTrue(event.is_set())

    async def test_battle_regular_ball_matching(self):
        import config
        from core.pokedex import PokedexService
        from handlers.battle_handler import BattleHandler

        pokedex = PokedexService(config.POKEDEX_FILE)
        battle_handler = BattleHandler(pokedex)
        config.BATTLE_SYSTEM = "hybrid"

        mock_msg = MagicMock()
        mock_msg.raw_text = (
            "Battle begins\n\n"
            "Wild Pidgey [Normal/Flying]\n"
            "Lv. 3 • HP 15/15\n\n"
            "Current turn: Shivam\n"
            "Charmander [Fire]"
        )

        btn_regular = MagicMock(text="Regular Ball", click=AsyncMock())
        btn_run = MagicMock(text="Run", click=AsyncMock())
        mock_msg.buttons = [[btn_regular, btn_run]]

        mock_client = AsyncMock()
        await battle_handler.handle_battle_turn(mock_client, "test_account", mock_msg)

        # Must detect and click Regular Ball
        btn_regular.click.assert_called_once()

    @patch("aiohttp.ClientSession.post")
    async def test_gemini_vision_silhouette_solver(self, mock_post):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": '{"pokemon": "Shellder", "confidence": 1.0, "reasoning": "Classic shell silhouette"}'}
                        ]
                    }
                }
            ]
        })
        mock_post.return_value.__aenter__.return_value = mock_resp

        solver = GeminiSolver(api_key="AIzaSyMockKeyForTesting12345", model="gemini-2.5-flash")
        poke, conf, reason = await solver.solve_pokemon_silhouette(b"fake_image_bytes")
        self.assertEqual(poke, "Shellder")
        self.assertEqual(conf, 1.0)

    def test_sprite_matcher_silhouettes(self):
        import config
        from core.sprite_matcher import SpriteMatcher
        matcher = SpriteMatcher(config.SPRITE_CACHE_FILE)
        self.assertGreater(len(matcher.cache), 1000)

        # Test on actual user silhouette if available
        sample_img = Path(r"C:\Users\kalaw\.gemini\antigravity-ide\brain\d54bfd7e-a95f-40ed-9daa-e6925a345496\.user_uploaded\media_1791204089543.png")
        if sample_img.exists():
            name, conf = matcher.match_silhouette(sample_img.read_bytes())
            self.assertEqual(name, "Glimmora")
            self.assertGreaterEqual(conf, 0.95)

    async def test_guess_handler_tier1_instant_match(self):
        from handlers.guess_handler import GuessHandler
        from core.sprite_matcher import SpriteMatcher

        mock_gemini = MagicMock()
        mock_gemini.solve_pokemon_silhouette = AsyncMock()

        mock_matcher = MagicMock(spec=SpriteMatcher)
        mock_matcher.match_silhouette.return_value = ("Glimmora", 0.99)

        on_end_mock = MagicMock()
        guess_handler = GuessHandler(
            gemini_solver=mock_gemini,
            sprite_matcher=mock_matcher,
            on_guess_end=on_end_mock
        )

        mock_msg = MagicMock()
        mock_msg.raw_text = "Who's that Pokémon?"
        mock_msg.media = MagicMock()
        mock_msg.download_media = AsyncMock(return_value=b"fake_sil_bytes")
        mock_msg.reply = AsyncMock()

        mock_client = AsyncMock()
        success = await guess_handler.handle_challenge(mock_client, "test_account", mock_msg)
        self.assertTrue(success)
        mock_matcher.match_silhouette.assert_called_once_with(b"fake_sil_bytes")
        # Tier 1 succeeded -> Gemini must NOT be called!
        mock_gemini.solve_pokemon_silhouette.assert_not_called()
        mock_msg.reply.assert_called_once_with("Glimmora")

    async def test_guess_handler_tier2_gemini_fallback(self):
        from handlers.guess_handler import GuessHandler
        from core.sprite_matcher import SpriteMatcher

        mock_gemini = MagicMock()
        mock_gemini.solve_pokemon_silhouette = AsyncMock(return_value=("Pikachu", 0.95, "Silhouette match"))

        mock_matcher = MagicMock(spec=SpriteMatcher)
        mock_matcher.match_silhouette.return_value = (None, 0.0)

        on_end_mock = MagicMock()
        guess_handler = GuessHandler(
            gemini_solver=mock_gemini,
            sprite_matcher=mock_matcher,
            on_guess_end=on_end_mock
        )

        mock_msg = MagicMock()
        mock_msg.raw_text = "Who's that Pokémon?"
        mock_msg.media = MagicMock()
        mock_msg.download_media = AsyncMock(return_value=b"fake_sil_bytes")
        mock_msg.reply = AsyncMock()

        mock_client = AsyncMock()
        success = await guess_handler.handle_challenge(mock_client, "test_account", mock_msg)
        self.assertTrue(mock_matcher.match_silhouette.called)
        # Tier 1 failed -> Fallback to Gemini Tier 2
        mock_gemini.solve_pokemon_silhouette.assert_called_once_with(b"fake_sil_bytes")
        mock_msg.reply.assert_called_once_with("Pikachu")

    async def test_guess_handler_and_reward(self):
        from handlers.guess_handler import GuessHandler

        mock_solver = MagicMock()
        mock_solver.solve_pokemon_silhouette = AsyncMock(return_value=("Shellder", 1.0, "Shell shape"))
        on_end_mock = MagicMock()
        guess_handler = GuessHandler(gemini_solver=mock_solver, on_guess_end=on_end_mock)

        # Mock 'Who's that Pokémon?' challenge message with photo
        mock_msg = MagicMock()
        mock_msg.raw_text = "Who's that Pokémon?"
        mock_msg.media = MagicMock()
        mock_msg.download_media = AsyncMock(return_value=b"fake_image_data")
        mock_msg.reply = AsyncMock()

        self.assertTrue(guess_handler.is_guess_challenge(mock_msg))

        mock_client = AsyncMock()
        success = await guess_handler.handle_challenge(mock_client, "test_account", mock_msg)
        self.assertTrue(success)
        mock_msg.reply.assert_called_once_with("Shellder")

        # Test reward / correct result
        result_msg = MagicMock()
        result_msg.raw_text = "Correct! It's Shellder! You gained 500 PD!"
        self.assertTrue(guess_handler.is_guess_result(result_msg))
        guess_handler.handle_result("test_account", result_msg)
        self.assertEqual(guess_handler.stats["correct_guesses"], 1)
        self.assertEqual(guess_handler.stats["pd_earned"], 500)
        on_end_mock.assert_called_once_with("test_account")

    async def test_battle_multi_turn_kill_loop(self):
        """Tests that BattleHandler continuously clicks moves across multiple turns until Pokémon dies."""
        import config
        from core.pokedex import PokedexService
        from handlers.battle_handler import BattleHandler

        pokedex = PokedexService(config.POKEDEX_FILE)
        on_end_mock = MagicMock()
        battle_handler = BattleHandler(pokedex, on_battle_end=on_end_mock)
        config.BATTLE_SYSTEM = "kill"

        mock_client = AsyncMock()

        # Turn 1: Metapod at full HP (27/27)
        turn1_msg = MagicMock()
        turn1_msg.id = 101
        turn1_msg.chat_id = 999
        turn1_msg.raw_text = (
            "Battle begins\n\n"
            "Wild Metapod [Bug]\n"
            "Lv. 8 • HP 27/27\n\n"
            "Current turn: Shivam\n"
            "Charmander [Fire]\n"
            "Lv. 5 • HP 19/19"
        )
        btn_scratch_t1 = MagicMock(text="Scratch (35/35)", click=AsyncMock())
        btn_ember_t1 = MagicMock(text="🔥 Ember (25/25)", click=AsyncMock())
        btn_run_t1 = MagicMock(text="Run", click=AsyncMock())
        turn1_msg.buttons = [[btn_scratch_t1, btn_ember_t1], [btn_run_t1]]

        self.assertTrue(battle_handler.is_battle_message(turn1_msg, "acc1"))
        await battle_handler.handle_battle_turn(mock_client, "acc1", turn1_msg)

        # In Kill Mode, Ember must be clicked on Turn 1!
        btn_ember_t1.click.assert_called_once()
        btn_scratch_t1.click.assert_not_called()
        self.assertIn("acc1", battle_handler.active_battles)

        # Turn 2: Metapod survives with 13/27 HP (battle text changes)
        turn2_msg = MagicMock()
        turn2_msg.id = 101
        turn2_msg.chat_id = 999
        turn2_msg.raw_text = (
            "Charmander used Ember! It's super effective!\n"
            "Wild Metapod lost 14 HP!\n\n"
            "Wild Metapod [Bug]\n"
            "Lv. 8 • HP: 13/27\n\n"
            "Charmander [Fire]\n"
            "Lv. 5 • HP 19/19"
        )
        btn_scratch_t2 = MagicMock(text="Scratch (35/35)", click=AsyncMock())
        btn_ember_t2 = MagicMock(text="🔥 Ember (24/25)", click=AsyncMock())
        btn_run_t2 = MagicMock(text="Run", click=AsyncMock())
        turn2_msg.buttons = [[btn_scratch_t2, btn_ember_t2], [btn_run_t2]]

        self.assertTrue(battle_handler.is_battle_message(turn2_msg, "acc1"))
        self.assertFalse(battle_handler.is_battle_end(turn2_msg))
        await battle_handler.handle_battle_turn(mock_client, "acc1", turn2_msg)

        # In Turn 2, Ember must be clicked again!
        btn_ember_t2.click.assert_called_once()
        btn_scratch_t2.click.assert_not_called()

        # Turn 3: Metapod faints! Battle ends!
        turn3_msg = MagicMock()
        turn3_msg.id = 101
        turn3_msg.chat_id = 999
        turn3_msg.raw_text = "Wild Metapod fainted! You gained 180 exp and 420 PD!"
        turn3_msg.buttons = []

        self.assertTrue(battle_handler.is_battle_end(turn3_msg))
        battle_handler.handle_battle_result("acc1", turn3_msg)

        # Active battle cleared & instant on_battle_end dispatched for next /hunt!
        self.assertNotIn("acc1", battle_handler.active_battles)
        on_end_mock.assert_called_once_with("acc1")
        self.assertEqual(battle_handler.stats["pd_earned"], 420)
        self.assertEqual(battle_handler.stats["battles_won"], 1)

    def test_spawn_does_not_intercept_battle(self):
        """Ensures SpawnHandler does not steal battle messages with utility/move buttons."""
        from handlers.spawn_handler import SpawnHandler

        spawn_handler = SpawnHandler()
        battle_msg = MagicMock()
        battle_msg.raw_text = "Wild Metapod [Bug] Lv. 8 • HP: 13/27"
        btn_move = MagicMock(text="Scratch")
        btn_run = MagicMock(text="Run")
        battle_msg.buttons = [[btn_move], [btn_run]]

        self.assertFalse(spawn_handler.is_spawn_message(battle_msg))

    async def test_hunt_categories_kill_vs_catch(self):
        """Verifies explicit behavior of Category 1 (Kill) vs Category 2 (Catch)."""
        import config
        from core.pokedex import PokedexService
        from handlers.battle_handler import BattleHandler

        pokedex = PokedexService(config.POKEDEX_FILE)
        battle_handler = BattleHandler(pokedex)
        mock_client = AsyncMock()

        # Encounter with wild Pidgey
        msg = MagicMock()
        msg.id = 202
        msg.chat_id = 777
        msg.raw_text = "Wild Pidgey [Normal/Flying]\nLv. 4 • HP: 16/16"
        btn_scratch = MagicMock(text="Scratch", click=AsyncMock())
        btn_ball = MagicMock(text="Poke Ball", click=AsyncMock())
        msg.buttons = [[btn_scratch], [btn_ball]]

        # Category 1: KILL
        config.HUNT_MODE = "kill"
        config.BATTLE_SYSTEM = "kill"
        await battle_handler.handle_battle_turn(mock_client, "acc", msg)
        btn_scratch.click.assert_called_once()
        btn_ball.click.assert_not_called()

        btn_scratch.click.reset_mock()
        btn_ball.click.reset_mock()

        # Category 2: CATCH
        config.HUNT_MODE = "catch"
        config.BATTLE_SYSTEM = "catch"
        await battle_handler.handle_battle_turn(mock_client, "acc", msg)
        btn_ball.click.assert_called_once()
        btn_scratch.click.assert_not_called()

    async def test_spawn_star_repeat_ball_priority(self):
        """Verifies that '☆' star on wild encounter forces Repeat Ball selection over Master/Ultra."""
        import config
        from handlers.spawn_handler import SpawnHandler

        spawn_handler = SpawnHandler()
        config.HUNT_MODE = "catch"
        config.BATTLE_SYSTEM = "catch"
        config.PREFERRED_BALLS = ["Masterball", "Ultraball", "Greatball", "Repeatball", "Pokeball"]

        msg = MagicMock()
        msg.raw_text = "A wild Pidgey ☆ appeared! Level 14."
        btn_master = MagicMock(text="Masterball (x1)", click=AsyncMock())
        btn_ultra = MagicMock(text="Ultraball (x5)", click=AsyncMock())
        btn_repeat = MagicMock(text="Repeatball (x12)", click=AsyncMock())
        btn_poke = MagicMock(text="Pokeball (x20)", click=AsyncMock())
        msg.buttons = [[btn_master, btn_ultra], [btn_repeat, btn_poke]]

        mock_client = AsyncMock()
        success = await spawn_handler.handle_spawn(mock_client, "acc1", msg)
        self.assertTrue(success)

        # MUST click Repeat Ball because of '☆' star!
        btn_repeat.click.assert_called_once()
        btn_master.click.assert_not_called()
        btn_ultra.click.assert_not_called()
        btn_poke.click.assert_not_called()

    async def test_spawn_star_missing_repeat_falls_back(self):
        """Verifies that if Repeat Ball is missing, star encounter falls back to preferred balls."""
        import config
        from handlers.spawn_handler import SpawnHandler

        spawn_handler = SpawnHandler()
        config.HUNT_MODE = "catch"
        config.PREFERRED_BALLS = ["Ultraball", "Greatball", "Pokeball"]

        msg = MagicMock()
        msg.raw_text = "Wild Rattata ★ appeared! Level 6."
        btn_ultra = MagicMock(text="Ultraball (x3)", click=AsyncMock())
        btn_poke = MagicMock(text="Pokeball (x15)", click=AsyncMock())
        msg.buttons = [[btn_ultra, btn_poke]]

        mock_client = AsyncMock()
        success = await spawn_handler.handle_spawn(mock_client, "acc1", msg)
        self.assertTrue(success)

        # Falls back to Ultraball
        btn_ultra.click.assert_called_once()
        btn_poke.click.assert_not_called()

    async def test_spawn_kill_mode_clicks_battle_button(self):
        """Verifies that in Kill mode, SpawnHandler clicks Battle button instead of throwing a ball."""
        import config
        from handlers.spawn_handler import SpawnHandler
        from handlers.battle_handler import BattleHandler
        from core.pokedex import PokedexService

        pokedex = PokedexService(config.POKEDEX_FILE)
        battle_handler = BattleHandler(pokedex)
        spawn_handler = SpawnHandler(battle_handler=battle_handler)

        config.HUNT_MODE = "kill"
        config.BATTLE_SYSTEM = "kill"

        msg = MagicMock()
        msg.id = 301
        msg.chat_id = 888
        msg.raw_text = "A wild Caterpie appeared!"
        btn_battle = MagicMock(text="⚔️ Battle", click=AsyncMock())
        btn_poke = MagicMock(text="Poke Ball", click=AsyncMock())
        msg.buttons = [[btn_battle], [btn_poke]]

        # Mock client to return an active battle message on re-fetch
        battle_screen = MagicMock()
        battle_screen.id = 301
        battle_screen.chat_id = 888
        battle_screen.raw_text = "Battle begins\nWild Caterpie [Bug] Lv. 3 • HP 12/12"
        btn_tackle = MagicMock(text="Tackle", click=AsyncMock())
        battle_screen.buttons = [[btn_tackle]]

        mock_client = AsyncMock()
        mock_client.get_messages.return_value = battle_screen

        success = await spawn_handler.handle_spawn(mock_client, "acc1", msg)
        self.assertTrue(success)

        btn_battle.click.assert_called()
        btn_poke.click.assert_not_called()

    async def test_click_battle_button_until_started_retries(self):
        """Verifies that click_battle_button_until_started keeps clicking until battle begins."""
        import config
        from handlers.spawn_handler import SpawnHandler
        from handlers.battle_handler import BattleHandler
        from core.pokedex import PokedexService

        pokedex = PokedexService(config.POKEDEX_FILE)
        battle_handler = BattleHandler(pokedex)
        spawn_handler = SpawnHandler(battle_handler=battle_handler)

        msg = MagicMock()
        msg.id = 401
        msg.chat_id = 999
        msg.raw_text = "A wild Pidgey appeared!"
        battle_btn = MagicMock(text="Battle", click=AsyncMock())
        msg.buttons = [[battle_btn]]

        # Attempt 1: get_messages returns un-updated message (still has Battle button)
        unupdated_msg = MagicMock()
        unupdated_msg.id = 401
        unupdated_msg.raw_text = "A wild Pidgey appeared!"
        unupdated_msg.buttons = [[battle_btn]]

        # Attempt 2: get_messages returns transitioned battle message
        started_msg = MagicMock()
        started_msg.id = 401
        started_msg.chat_id = 999
        started_msg.raw_text = "Battle begins!\nWild Pidgey Lv. 4 • HP 15/15"
        started_msg.buttons = [[MagicMock(text="Scratch", click=AsyncMock())]]

        mock_client = AsyncMock()
        # First call returns unupdated_msg, second call returns started_msg
        mock_client.get_messages.side_effect = [unupdated_msg, started_msg]

        success = await spawn_handler.click_battle_button_until_started(
            mock_client, "acc1", msg, battle_btn, max_attempts=5, click_interval=0.01
        )
        self.assertTrue(success)
        # Should have clicked at least twice before transition was detected!
        self.assertGreaterEqual(battle_btn.click.call_count, 2)

if __name__ == "__main__":
    unittest.main()






