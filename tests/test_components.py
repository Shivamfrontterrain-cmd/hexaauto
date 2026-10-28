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

    async def test_clefairy_wild_encounter_with_star_and_ev_yield_clicks_battle_then_repeat_ball(self):
        """
        Exact scenario from user screenshot:
        1. Spawn: 'A wild Clefairy (Lv. 8) has appeared ☆' with buttons [Battle] | [EV Yield].
        2. Must recognize as spawn, not active battle.
        3. Must click [Battle], NOT [EV Yield].
        4. When battle screen updates, must use Repeat Ball due to star '☆'.
        """
        import config
        from handlers.spawn_handler import SpawnHandler
        from handlers.battle_handler import BattleHandler
        from core.pokedex import PokedexService

        pokedex = PokedexService(config.POKEDEX_FILE)
        battle_handler = BattleHandler(pokedex)
        spawn_handler = SpawnHandler(battle_handler=battle_handler)

        config.HUNT_MODE = "catch"
        config.BATTLE_SYSTEM = "catch"

        # 1. Initial Wild Spawn message
        spawn_msg = MagicMock()
        spawn_msg.id = 555
        spawn_msg.chat_id = 12345
        spawn_msg.raw_text = "A wild Clefairy (Lv. 8) has appeared ☆"
        btn_battle = MagicMock(text="Battle", click=AsyncMock())
        btn_ev_yield = MagicMock(text="EV Yield", click=AsyncMock())
        spawn_msg.buttons = [[btn_battle, btn_ev_yield]]

        # Verify message classification
        self.assertTrue(spawn_handler.is_spawn_message(spawn_msg))
        self.assertFalse(battle_handler.is_battle_message(spawn_msg, "acc1"))

        # 2. Battle Screen message returned after clicking Battle
        battle_msg = MagicMock()
        battle_msg.id = 555
        battle_msg.chat_id = 12345
        battle_msg.raw_text = "Battle begins\nWild Clefairy [Fairy]\nLv. 8 • HP 35/35"
        btn_repeat = MagicMock(text="Repeat Ball x8", click=AsyncMock())
        btn_poke = MagicMock(text="Poke Ball x15", click=AsyncMock())
        btn_run = MagicMock(text="Run", click=AsyncMock())
        battle_msg.buttons = [[btn_repeat, btn_poke], [btn_run]]

        mock_client = AsyncMock()
        mock_client.get_messages.return_value = battle_msg

        # Handle spawn
        success = await spawn_handler.handle_spawn(mock_client, "acc1", spawn_msg)
        self.assertTrue(success)

        # Must click Battle button!
        btn_battle.click.assert_called()
        # Must NEVER click EV Yield!
        btn_ev_yield.click.assert_not_called()

        # In battle turn, must throw Repeat Ball because Clefairy has '☆' star!
        btn_repeat.click.assert_called_once()
        btn_poke.click.assert_not_called()

    async def test_auto_hunter_holds_hunt_until_battle_and_spawn_complete(self):
        """Verifies that AutoHunter is_encounter_active tracks both battle and spawn encounters."""
        from core.auto_hunter import AutoHunter
        from handlers.check_handler import CheckHandler
        from handlers.battle_handler import BattleHandler
        from handlers.spawn_handler import SpawnHandler
        from core.pokedex import PokedexService
        import config

        check_h = CheckHandler(MagicMock(), MagicMock(), MagicMock())
        pokedex = PokedexService(config.POKEDEX_FILE)
        battle_h = BattleHandler(pokedex)
        spawn_h = SpawnHandler(battle_handler=battle_h)
        hunter = AutoHunter(check_handler=check_h, battle_handler=battle_h, spawn_handler=spawn_h)

        # 1. Initially no active encounter
        self.assertFalse(hunter.is_encounter_active("acc1"))

        # 2. Spawn encounter active
        spawn_h.active_encounters["acc1"] = {"poke_name": "Pikachu"}
        self.assertTrue(hunter.is_encounter_active("acc1"))

        # 3. Transition to battle
        spawn_h.active_encounters.pop("acc1")
        battle_h.active_battles["acc1"] = {"wild_name": "Pikachu"}
        self.assertTrue(hunter.is_encounter_active("acc1"))

        # 4. Battle ends with mark_encounter_complete
        event = hunter.get_encounter_event("acc1")
        event.clear()
        hunter.mark_encounter_complete("acc1")
        self.assertFalse(hunter.is_encounter_active("acc1"))
        self.assertTrue(event.is_set())

    async def test_spearow_catch_success_releases_hunt_lock_and_allows_next_hunt(self):
        """Verifies that when a Pokémon is caught, the hunt lock is released and both handlers are cleared."""
        from core.auto_hunter import AutoHunter
        from handlers.check_handler import CheckHandler
        from handlers.battle_handler import BattleHandler
        from handlers.spawn_handler import SpawnHandler
        from core.pokedex import PokedexService
        import config

        check_h = CheckHandler(MagicMock(), MagicMock(), MagicMock())
        pokedex = PokedexService(config.POKEDEX_FILE)
        hunter = AutoHunter(check_handler=check_h)
        battle_h = BattleHandler(pokedex, on_battle_end=hunter.mark_encounter_complete)
        spawn_h = SpawnHandler(on_catch_end=hunter.mark_encounter_complete, battle_handler=battle_h)
        hunter.set_handlers(battle_handler=battle_h, spawn_handler=spawn_h)

        # Spearow spawn was active in spawn_h
        spawn_h.active_encounters["shivam"] = {"poke_name": "Spearow"}
        self.assertTrue(hunter.is_encounter_active("shivam"))

        event = hunter.get_encounter_event("shivam")
        event.clear()

        # Catch message arrives and triggers battle_h.handle_battle_result
        win_msg = MagicMock()
        win_msg.raw_text = "🎉 Caught new Pokémon! Spearow was captured!"
        battle_h.handle_battle_result("shivam", win_msg)

        # Must release hunt lock and clear active encounter!
        self.assertFalse(hunter.is_encounter_active("shivam"))
        self.assertTrue(event.is_set())

    async def test_spawn_handler_broke_free_with_buttons_does_not_release_lock(self):
        """Verifies that when a Pokémon breaks free but buttons remain, the encounter lock is preserved."""
        from handlers.spawn_handler import SpawnHandler

        on_catch_end = MagicMock()
        spawn_h = SpawnHandler(on_catch_end=on_catch_end)
        spawn_h.active_encounters["acc1"] = {"poke_name": "Clefairy"}

        # Message where Pokémon broke free, but buttons are still present!
        msg = MagicMock()
        msg.raw_text = "Wild Clefairy broke free!"
        btn_repeat = MagicMock(text="Repeat Ball")
        msg.buttons = [[btn_repeat]]

        mock_client = AsyncMock()
        # Mock handle_spawn
        spawn_h.handle_spawn = AsyncMock()

        spawn_h.handle_catch_result("acc1", msg, client=mock_client)

        # Encounter must still be active!
        self.assertIn("acc1", spawn_h.active_encounters)
        # on_catch_end must NOT have been called!
        on_catch_end.assert_not_called()
        # handle_spawn should have been scheduled
        await asyncio.sleep(0.01)
        spawn_h.handle_spawn.assert_called_once()

    async def test_catch_mode_with_electro_ball_move_does_not_attack_and_opens_bag(self):
        """
        Exact bug reported by user:
        Player is in Catch mode. Pokémon has 'Electro Ball' and 'Gyro Ball' moves.
        Moves must NOT be categorized as balls and must NOT be used to attack/kill the wild Pokémon.
        Instead, BattleHandler must open Bag to access Pokéballs.
        """
        import config
        from core.pokedex import PokedexService
        from handlers.battle_handler import BattleHandler

        pokedex = PokedexService(config.POKEDEX_FILE)
        battle_handler = BattleHandler(pokedex)

        config.HUNT_MODE = "catch"
        config.BATTLE_SYSTEM = "catch"
        config.HYBRID_KILL_COMMONS = False

        msg = MagicMock()
        msg.id = 1001
        msg.chat_id = 999
        msg.raw_text = "Battle begins\nWild Machop [Fighting]\nLv. 15 • HP 45/45\nCurrent turn: Shivam\nElectrode [Electric]"
        btn_electro = MagicMock(text="Electro Ball", click=AsyncMock())
        btn_gyro = MagicMock(text="Gyro Ball", click=AsyncMock())
        btn_bag = MagicMock(text="Bag", click=AsyncMock())
        btn_run = MagicMock(text="Run", click=AsyncMock())
        msg.buttons = [[btn_electro, btn_gyro], [btn_bag, btn_run]]

        moves, balls, utils, actions = battle_handler.categorize_buttons(msg)
        self.assertIn("Electro Ball", moves)
        self.assertIn("Gyro Ball", moves)
        self.assertEqual(len(balls), 0)
        self.assertIn("Bag", utils)

        mock_client = AsyncMock()
        await battle_handler.handle_battle_turn(mock_client, "test_account", msg)

        # Must NOT attack with Electro Ball or Gyro Ball!
        btn_electro.click.assert_not_called()
        btn_gyro.click.assert_not_called()
        # Must click Bag!
        btn_bag.click.assert_called_once()

    async def test_catch_mode_throws_pokeball_from_bag(self):
        """
        After Bag is opened, Pokéballs are presented as buttons.
        BattleHandler must choose the appropriate Pokéball and throw it.
        """
        import config
        from core.pokedex import PokedexService
        from handlers.battle_handler import BattleHandler

        pokedex = PokedexService(config.POKEDEX_FILE)
        battle_handler = BattleHandler(pokedex)

        config.HUNT_MODE = "catch"
        config.BATTLE_SYSTEM = "catch"
        config.HYBRID_KILL_COMMONS = False

        msg = MagicMock()
        msg.id = 1002
        msg.chat_id = 999
        msg.raw_text = "Select a ball to catch Wild Machop:"
        btn_poke = MagicMock(text="Poke Ball (x20)", click=AsyncMock())
        btn_great = MagicMock(text="Great Ball (x10)", click=AsyncMock())
        btn_back = MagicMock(text="<< Back", click=AsyncMock())
        msg.buttons = [[btn_poke, btn_great], [btn_back]]

        mock_client = AsyncMock()
        await battle_handler.handle_battle_turn(mock_client, "test_account", msg)

        # Must throw Poke Ball at common Machop!
        btn_poke.click.assert_called_once()
        btn_great.click.assert_not_called()
        btn_back.click.assert_not_called()

    async def test_kill_mode_with_electro_ball_attacks_as_move(self):
        """
        In Kill mode, Electro Ball is recognized as a move and used to defeat the wild Pokémon for PD.
        """
        import config
        from core.pokedex import PokedexService
        from handlers.battle_handler import BattleHandler

        pokedex = PokedexService(config.POKEDEX_FILE)
        battle_handler = BattleHandler(pokedex)

        config.HUNT_MODE = "kill"
        config.BATTLE_SYSTEM = "kill"

        msg = MagicMock()
        msg.id = 1003
        msg.chat_id = 999
        msg.raw_text = "Battle begins\nWild Squirtle [Water]\nLv. 10 • HP 30/30\nCurrent turn: Shivam\nElectrode [Electric]"
        btn_electro = MagicMock(text="Electro Ball", click=AsyncMock())
        btn_bag = MagicMock(text="Bag", click=AsyncMock())
        msg.buttons = [[btn_electro], [btn_bag]]

        mock_client = AsyncMock()
        await battle_handler.handle_battle_turn(mock_client, "test_account", msg)

        # Must use Electro Ball to attack Water-type Squirtle
        btn_electro.click.assert_called_once()
        btn_bag.click.assert_not_called()

    def test_is_pokeball_button_precision(self):
        """
        Verifies that moves with 'ball' are not balls and real Pokéballs are recognized.
        """
        import config
        from core.pokedex import PokedexService, is_pokeball_button

        pokedex = PokedexService(config.POKEDEX_FILE)

        # Moves must return False
        moves = [
            "Electro Ball", "Gyro Ball", "Shadow Ball", "Energy Ball",
            "Mist Ball", "Weather Ball", "Ice Ball", "Pyro Ball", "Scratch"
        ]
        for m in moves:
            self.assertFalse(is_pokeball_button(m, pokedex), f"Move '{m}' was incorrectly recognized as a Pokéball!")

        # Real Pokéballs must return True
        balls = [
            "Poke Ball", "Pokeball (x15)", "Poké Ball", "Regular Ball", "Regular",
            "Repeat Ball x8", "Ultraball (x5)", "Master Ball", "Great Ball", "Dusk Ball"
        ]
        for b in balls:
            self.assertTrue(is_pokeball_button(b, pokedex), f"Pokéball '{b}' was not recognized as a Pokéball!")

    def test_custom_emoji_star_id_detected_and_prioritizes_repeat_ball(self):
        """
        Exact user case (Image 2):
        'A wild Toxicroak (Lv. 53) has appeared! [custom emoji star]'
        Custom emoji ID is 5258185631355378853.
        Must accurately identify the star and prioritize Repeat Ball.
        """
        import config
        from core.pokedex import check_has_star
        from handlers.spawn_handler import SpawnHandler

        # Mock custom emoji entity from Telegram
        custom_emoji_ent = MagicMock()
        custom_emoji_ent.document_id = 5258185631355378853

        msg = MagicMock()
        msg.raw_text = "A wild Toxicroak (Lv. 53) has appeared!"
        msg.entities = [custom_emoji_ent]

        # 1. check_has_star must return True for custom emoji id 5258185631355378853
        self.assertTrue(check_has_star(msg), "Custom emoji star ID 5258185631355378853 was not detected!")

        # 2. SpawnHandler must select Repeat Ball for this encounter
        spawn_handler = SpawnHandler()
        btn_ultra = MagicMock(text="Ultraball (x10)", click=AsyncMock())
        btn_repeat = MagicMock(text="Repeatball (x5)", click=AsyncMock())
        btn_poke = MagicMock(text="Pokeball (x20)", click=AsyncMock())
        msg.buttons = [[btn_ultra], [btn_repeat, btn_poke]]

        mock_client = AsyncMock()
        loop = asyncio.get_event_loop()
        success = loop.run_until_complete(spawn_handler.handle_spawn(mock_client, "acc1", msg))
        self.assertTrue(success)
        btn_repeat.click.assert_called_once()
        btn_ultra.click.assert_not_called()
        btn_poke.click.assert_not_called()

    def test_custom_emoji_star_in_battle_prioritizes_repeat_ball_or_bag(self):
        """
        Verifies that BattleHandler detects custom emoji star (5258185631355378853)
        in battle turns and forces Repeat Ball catch (or Bag opening).
        """
        import config
        from core.pokedex import PokedexService
        from handlers.battle_handler import BattleHandler

        pokedex = PokedexService(config.POKEDEX_FILE)
        battle_h = BattleHandler(pokedex)

        custom_emoji_ent = MagicMock()
        custom_emoji_ent.document_id = 5258185631355378853

        msg = MagicMock()
        msg.raw_text = "Battle began!\nWild Toxicroak [Poison/Fighting]\nHP 120/120"
        msg.entities = [custom_emoji_ent]

        btn_bag = MagicMock(text="🎒 Bag", click=AsyncMock())
        btn_scratch = MagicMock(text="Scratch", click=AsyncMock())
        msg.buttons = [[btn_scratch], [btn_bag]]

        mock_client = AsyncMock()
        loop = asyncio.get_event_loop()

        # Should open Bag to access Repeat Ball because of star!
        loop.run_until_complete(battle_h.handle_battle_turn(mock_client, "acc1", msg))
        btn_bag.click.assert_called_once()
        btn_scratch.click.assert_not_called()

    def test_wild_pokemon_broke_free_in_battle_does_not_release_lock(self):
        """
        Exact user case (Image 1):
        'Aww! Wild Purugly broke free! Wild Purugly used Play Rough It dealt 10 damage!'
        Buttons: [Fight] [Bag] [Run]
        Encounter/battle lock MUST NOT be released, and turn must be routed to battle handler.
        """
        import config
        from core.pokedex import PokedexService
        from handlers.battle_handler import BattleHandler
        from handlers.spawn_handler import SpawnHandler
        from core.auto_hunter import AutoHunter
        from handlers.check_handler import CheckHandler

        pokedex = PokedexService(config.POKEDEX_FILE)
        check_h = CheckHandler(MagicMock(), MagicMock(), MagicMock())
        hunter = AutoHunter(check_handler=check_h)
        battle_h = BattleHandler(pokedex, on_battle_end=hunter.mark_encounter_complete)
        spawn_h = SpawnHandler(on_catch_end=hunter.mark_encounter_complete, battle_handler=battle_h)
        hunter.set_handlers(battle_handler=battle_h, spawn_handler=spawn_h)

        # Battle is actively in progress
        battle_h.active_battles["acc1"] = {"msg_id": 100, "wild_name": "Purugly"}
        event = hunter.get_encounter_event("acc1")
        event.clear()

        # Message where Purugly broke free and attacked
        msg = MagicMock()
        msg.raw_text = (
            "Aww! Wild Purugly broke free!\n\n"
            "Wild Purugly used Play Rough\n"
            "It dealt 10 damage!\n"
            "It's not very effective...\n\n"
            "Current turn: Shivam\n"
            "Purugly [Normal]\n"
            "HP: 45/60"
        )
        btn_fight = MagicMock(text="Fight", click=AsyncMock())
        btn_bag = MagicMock(text="Bag", click=AsyncMock())
        btn_run = MagicMock(text="Run", click=AsyncMock())
        msg.buttons = [[btn_fight, btn_bag, btn_run]]

        # 1. is_battle_message must be True
        self.assertTrue(battle_h.is_battle_message(msg, "acc1"))

        # 2. is_catch_result must be False for active battle turn
        self.assertFalse(spawn_h.is_catch_result(msg))

        # 3. Encounter must still be active and event must remain cleared (no /hunt allowed)
        self.assertTrue(hunter.is_encounter_active("acc1"))
        self.assertFalse(event.is_set())

    def test_wild_pokemon_fled_ends_battle_and_releases_lock(self):
        """
        Verifies that when 'Wild Purugly fled from the battle!' arrives,
        the battle conclusion is properly processed and releases hunt lock.
        """
        import config
        from core.pokedex import PokedexService
        from handlers.battle_handler import BattleHandler
        from core.auto_hunter import AutoHunter
        from handlers.check_handler import CheckHandler

        pokedex = PokedexService(config.POKEDEX_FILE)
        check_h = CheckHandler(MagicMock(), MagicMock(), MagicMock())
        hunter = AutoHunter(check_handler=check_h)
        battle_h = BattleHandler(pokedex, on_battle_end=hunter.mark_encounter_complete)

        battle_h.active_battles["acc1"] = {"msg_id": 100, "wild_name": "Purugly"}
        event = hunter.get_encounter_event("acc1")
        event.clear()

        fled_msg = MagicMock()
        fled_msg.raw_text = "Wild Purugly fled from the battle!"
        fled_msg.buttons = []

        # is_battle_end must detect fled
        self.assertTrue(battle_h.is_battle_end(fled_msg))

        battle_h.handle_battle_result("acc1", fled_msg)

        # Active battles must be cleared and hunt lock released
        self.assertNotIn("acc1", battle_h.active_battles)
        self.assertTrue(event.is_set())

    async def test_resolve_stuck_battle_when_bot_reports_cannot_hunt(self):
        """
        Verifies that when 'Cannot hunt while in battle!' arrives,
        resolve_stuck_battle handles pending turns or unlocks cleanly after waiting.
        """
        import config
        from core.pokedex import PokedexService
        from handlers.battle_handler import BattleHandler
        from handlers.spawn_handler import SpawnHandler
        from core.client_manager import ClientManager
        from core.auto_hunter import AutoHunter
        from handlers.check_handler import CheckHandler

        pokedex = PokedexService(config.POKEDEX_FILE)
        check_h = CheckHandler(MagicMock(), MagicMock(), MagicMock())
        hunter = AutoHunter(check_handler=check_h)
        battle_h = BattleHandler(pokedex, on_battle_end=hunter.mark_encounter_complete)
        spawn_h = SpawnHandler(on_catch_end=hunter.mark_encounter_complete, battle_handler=battle_h)

        manager = ClientManager(
            check_handler=check_h,
            spawn_handler=spawn_h,
            battle_handler=battle_h,
            auto_hunter=hunter
        )

        mock_client = AsyncMock()
        # Case A: An active battle message exists in chat
        active_turn_msg = MagicMock()
        active_turn_msg.id = 55
        active_turn_msg.raw_text = "Wild Purugly [Normal]\nHP: 30/60\nYour turn"
        btn_scratch = MagicMock(text="Scratch", click=AsyncMock())
        active_turn_msg.buttons = [[btn_scratch]]

        mock_client.get_messages = AsyncMock(return_value=[active_turn_msg])
        battle_h.handle_battle_turn = AsyncMock()

        await manager.resolve_stuck_battle(mock_client, "acc1", 12345)
        battle_h.handle_battle_turn.assert_called_once_with(mock_client, "acc1", active_turn_msg)

    async def test_tangrowth_wild_star_encounter_uses_repeat_ball(self):
        """
        Exact user case:
        'A wild Tangrowth (Lv. 48) has appeared! ☆'
        Ensures star emoji at the end of the spawn message is detected and Repeat Ball is clicked.
        """
        import config
        from core.pokedex import check_has_star, is_pokeball_button
        from handlers.spawn_handler import SpawnHandler

        msg = MagicMock()
        msg.id = 888
        msg.chat_id = 123
        msg.raw_text = "A wild Tangrowth (Lv. 48) has appeared! ☆"

        # 1. Star check
        self.assertTrue(check_has_star(msg), "Star emoji '☆' at end of Tangrowth spawn was not detected!")

        # 2. Button recognition
        self.assertTrue(is_pokeball_button("Repeat (x15)"), "'Repeat (x15)' was not recognized as a Pokéball!")
        self.assertTrue(is_pokeball_button("Repeat"), "'Repeat' was not recognized as a Pokéball!")
        self.assertTrue(is_pokeball_button("Repeat Ball"), "'Repeat Ball' was not recognized as a Pokéball!")

        # 3. SpawnHandler throws Repeat Ball
        spawn_handler = SpawnHandler()
        btn_ultra = MagicMock(text="Ultraball (x5)", click=AsyncMock())
        btn_repeat = MagicMock(text="Repeat (x15)", click=AsyncMock())
        btn_poke = MagicMock(text="Pokeball (x20)", click=AsyncMock())
        msg.buttons = [[btn_ultra], [btn_repeat, btn_poke]]

        mock_client = AsyncMock()
        success = await spawn_handler.handle_spawn(mock_client, "acc1", msg)
        self.assertTrue(success)
        btn_repeat.click.assert_called_once()
        btn_ultra.click.assert_not_called()
        btn_poke.click.assert_not_called()

    async def test_tangrowth_star_encounter_in_battle_prioritizes_bag_when_balls_present(self):
        """
        Verifies that in battle vs a starred Pokémon (Tangrowth ☆), if Poke Ball is on screen
        but Repeat Ball is in Bag, BattleHandler opens Bag instead of prematurely throwing Poke Ball.
        """
        import config
        from core.pokedex import PokedexService
        from handlers.battle_handler import BattleHandler

        pokedex = PokedexService(config.POKEDEX_FILE)
        battle_handler = BattleHandler(pokedex)

        # Pre-mark encounter as starred from spawn
        battle_handler.starred_encounters["acc1"] = True

        msg = MagicMock()
        msg.id = 991
        msg.raw_text = "Battle began!\nWild Tangrowth [Grass]\nHP: 140/140"
        btn_poke = MagicMock(text="Poke Ball (x10)", click=AsyncMock())
        btn_bag = MagicMock(text="🎒 Bag", click=AsyncMock())
        btn_run = MagicMock(text="Run", click=AsyncMock())
        msg.buttons = [[btn_poke], [btn_bag, btn_run]]

        mock_client = AsyncMock()
        await battle_handler.handle_battle_turn(mock_client, "acc1", msg)

        # Must click Bag to find Repeat Ball! Must NOT throw Poke Ball!
        btn_bag.click.assert_called_once()
        btn_poke.click.assert_not_called()

    async def test_star_encounter_persists_after_pokemon_breaks_free(self):
        """
        Verifies that when a starred Pokémon breaks free, the encounter retains its star status
        and re-attempts catch using Repeat Ball on the next turn.
        """
        from handlers.spawn_handler import SpawnHandler

        spawn_handler = SpawnHandler()

        # Step 1: Initial spawn with '☆'
        msg1 = MagicMock()
        msg1.id = 501
        msg1.chat_id = 999
        msg1.raw_text = "A wild Tangrowth (Lv. 48) has appeared! ☆"
        btn_repeat1 = MagicMock(text="Repeat Ball (x8)", click=AsyncMock())
        msg1.buttons = [[btn_repeat1]]

        mock_client = AsyncMock()
        await spawn_handler.handle_spawn(mock_client, "acc1", msg1)
        self.assertTrue(spawn_handler.starred_encounters.get("acc1", False))

        # Step 2: Pokémon breaks free (text no longer mentions star)
        break_free_msg = MagicMock()
        break_free_msg.id = 501
        break_free_msg.chat_id = 999
        break_free_msg.raw_text = "Wild Tangrowth broke free! What will you do?"
        btn_repeat2 = MagicMock(text="Repeat Ball (x7)", click=AsyncMock())
        btn_ultra2 = MagicMock(text="Ultra Ball (x10)", click=AsyncMock())
        break_free_msg.buttons = [[btn_ultra2], [btn_repeat2]]

        # Re-attempt spawn
        await spawn_handler.handle_spawn(mock_client, "acc1", break_free_msg)

        # Star must persist and Repeat Ball must be clicked again!
        self.assertTrue(spawn_handler.starred_encounters.get("acc1", False))
        btn_repeat2.click.assert_called_once()
        btn_ultra2.click.assert_not_called()

    def test_various_star_unicode_symbols_detected(self):
        """
        Tests that check_has_star detects all common Unicode star and emoji symbols.
        """
        from core.pokedex import check_has_star

        stars = ["☆", "★", "⭐", "⭐️", "🌟", "✨", "💫", "✪", "✫", "✬", "✭", "✮", "✯", "✰"]
        for s in stars:
            msg = MagicMock()
            msg.raw_text = f"A wild Tangrowth (Lv. 48) has appeared! {s}"
            msg.entities = []
            self.assertTrue(check_has_star(msg), f"Star symbol '{s}' was not detected!")

    def test_custom_emoji_with_unknown_doc_id_in_wild_spawn_detected_as_star(self):
        """
        Tests that any custom emoji entity in a wild spawn message is recognized as the star.
        """
        from core.pokedex import check_has_star

        ent = MagicMock()
        ent.document_id = 999888777666  # Arbitrary new emoji ID from bot pack
        msg = MagicMock()
        msg.raw_text = "A wild Tangrowth (Lv. 48) has appeared!"
        msg.entities = [ent]

        self.assertTrue(check_has_star(msg), "Custom emoji in wild spawn was not recognized as star!")

    def test_is_pokeball_menu_button_distinction(self):
        """
        Tests that menu opener buttons ('Pokeballs', 'Balls', 'Bag') are distinguished from
        throwable balls ('Repeat (x15)', 'Poke Ball (x50)') and item buttons ('Item').
        """
        from core.pokedex import is_pokeball_button, is_pokeball_menu_button

        menu_buttons = ["Pokeballs", "Pokéballs", "Poke Balls", "Poké Balls", "Balls", "Throw Ball", "Bag", "🎒 Bag", "🎒"]
        for mb in menu_buttons:
            self.assertTrue(is_pokeball_menu_button(mb), f"'{mb}' was not recognized as a Pokéball menu button!")
            self.assertFalse(is_pokeball_button(mb), f"Menu button '{mb}' was incorrectly recognized as a throwable ball!")

        throwable_balls = ["Repeat Ball", "Repeat (x15)", "Repeat", "Poke Ball (x50)", "Pokeball (x20)", "Ultra Ball", "Ultraball (x5)", "Greatball"]
        for tb in throwable_balls:
            self.assertFalse(is_pokeball_menu_button(tb), f"Throwable ball '{tb}' was incorrectly recognized as a menu button!")
            self.assertTrue(is_pokeball_button(tb), f"Throwable ball '{tb}' was not recognized as a throwable Pokéball!")

        non_ball_buttons = ["Item", "Items", "Switch", "Run", "Electro Ball", "Gyro Ball"]
        for nb in non_ball_buttons:
            self.assertFalse(is_pokeball_menu_button(nb), f"'{nb}' should not be a Pokéball menu button!")
            self.assertFalse(is_pokeball_button(nb), f"'{nb}' should not be a throwable Pokéball!")

    async def test_florges_star_battle_clicks_pokeballs_menu_not_item(self):
        """
        Exact user glitch scenario:
        Wild : ☆ Florges (Orange Flower) [ Fairy 🌸 ]
        Level : 36 | HP : 109/109
        Corviknight [ Flying / Steel ] (Lv 100)
        Moves: Giga Impact, Heavy Slam, Steel Beam, Brave Bird
        Row 3: Switch, Run, Pokeballs
        Row 4: Item

        Must click [Pokeballs] to open ball menu!
        Must NOT click [Item]!
        Must NOT attack with [Giga Impact]!
        """
        import config
        from core.pokedex import PokedexService
        from handlers.battle_handler import BattleHandler

        pokedex = PokedexService(config.POKEDEX_FILE)
        battle_handler = BattleHandler(pokedex)

        msg = MagicMock()
        msg.id = 8881
        msg.chat_id = 999
        msg.raw_text = (
            "A wild Florges (Orange Flower) (Lv. 36) has appeared! ☆\n\n"
            "Wild : ☆ Florges (Orange Flower) [ Fairy 🌸 ]\n"
            "Level : 36 | HP : 109/109\n\n"
            "Turn : None\n"
            "Corviknight [ Flying / Steel ]\n"
            "Level : 100 | HP : 375/375\n\n"
            "• Giga Impact [ Normal ]\n"
            "• Heavy Slam [ Steel ]\n"
            "• Steel Beam [ Steel ]\n"
            "• Brave Bird [ Flying ]"
        )
        btn_giga = MagicMock(text="Giga Impact", click=AsyncMock())
        btn_heavy = MagicMock(text="Heavy Slam", click=AsyncMock())
        btn_beam = MagicMock(text="Steel Beam", click=AsyncMock())
        btn_bird = MagicMock(text="Brave Bird", click=AsyncMock())
        btn_switch = MagicMock(text="Switch", click=AsyncMock())
        btn_run = MagicMock(text="Run", click=AsyncMock())
        btn_pokeballs = MagicMock(text="Pokeballs", click=AsyncMock())
        btn_item = MagicMock(text="Item", click=AsyncMock())

        msg.buttons = [
            [btn_giga, btn_heavy],
            [btn_beam, btn_bird],
            [btn_switch, btn_run, btn_pokeballs],
            [btn_item]
        ]

        # Step 1: Categorize buttons
        moves, balls, utils, actions = battle_handler.categorize_buttons(msg)
        self.assertIn("Giga Impact", moves)
        self.assertIn("Pokeballs", utils)
        self.assertIn("Item", utils)
        self.assertNotIn("Pokeballs", balls, "Pokeballs menu button must not be categorized as a throwable ball!")
        self.assertEqual(len(balls), 0)

        # Step 2: Handle battle turn
        mock_client = AsyncMock()
        await battle_handler.handle_battle_turn(mock_client, "acc_florges", msg)

        # Must click Pokeballs!
        btn_pokeballs.click.assert_called_once()
        # Must NOT click Item!
        btn_item.click.assert_not_called()
        # Must NOT attack!
        btn_giga.click.assert_not_called()
        btn_heavy.click.assert_not_called()

        # Step 3: Next turn after clicking Pokeballs, submenu is open
        msg_submenu = MagicMock()
        msg_submenu.id = 8881
        msg_submenu.chat_id = 999
        msg_submenu.raw_text = msg.raw_text

        btn_poke = MagicMock(text="Poke Ball (x50)", click=AsyncMock())
        btn_great = MagicMock(text="Great Ball (x20)", click=AsyncMock())
        btn_ultra = MagicMock(text="Ultra Ball (x10)", click=AsyncMock())
        btn_repeat = MagicMock(text="Repeat Ball (x15)", click=AsyncMock())
        btn_back = MagicMock(text="Back", click=AsyncMock())

        msg_submenu.buttons = [
            [btn_poke, btn_great],
            [btn_ultra, btn_repeat],
            [btn_back]
        ]

        await battle_handler.handle_battle_turn(mock_client, "acc_florges", msg_submenu)

        # Must throw Repeat Ball!
        btn_repeat.click.assert_called_once()
        btn_poke.click.assert_not_called()
        btn_back.click.assert_not_called()

    async def test_auto_switch_on_fainted_active_pokemon(self):
        """
        Tests the exact scenario where the wild Pokémon defeats player's active Pokémon
        and Hexa bot prompts 'Choose a Pokémon to switch to:'.
        Auto-switch must:
        1. NOT treat this as a battle end / victory
        2. Recognize it as an active battle switch turn
        3. Parse the alive party members and pick the best matchup
        4. Click the alive button (e.g. Primarina with 4.0x Water vs Arcanine-hisui Fire/Rock)
        5. NEVER click the empty/fainted button or Back/View Team
        """
        import config
        from core.pokedex import PokedexService
        from handlers.battle_handler import BattleHandler

        pokedex = PokedexService(config.POKEDEX_FILE)
        battle_handler = BattleHandler(pokedex)

        switch_msg = MagicMock()
        switch_msg.id = 5555
        switch_msg.chat_id = 999
        switch_msg.raw_text = (
            "Aww! Wild Arcanine-hisui broke free!\n\n"
            "Wild Arcanine-hisui used Flare Blitz\n"
            "It dealt 4 damage!\n"
            "It's super effective!\n"
            "Metagross fainted!\n\n"
            "Choose a Pokémon to switch to:\n\n"
            "1. Metagross (Fainted)\n"
            "2. Delphox\n"
            "3. Primarina\n"
            "4. Gengar\n"
            "5. Iron Valiant\n"
            "6. Corviknight"
        )

        btn_empty = MagicMock(text=" ", click=AsyncMock())
        btn_delphox = MagicMock(text="2", click=AsyncMock())
        btn_primarina = MagicMock(text="3", click=AsyncMock())
        btn_gengar = MagicMock(text="4", click=AsyncMock())
        btn_iron_valiant = MagicMock(text="5", click=AsyncMock())
        btn_corviknight = MagicMock(text="6", click=AsyncMock())
        btn_view_team = MagicMock(text="View Team", click=AsyncMock())
        btn_back = MagicMock(text="Back", click=AsyncMock())

        switch_msg.buttons = [
            [btn_empty, btn_delphox],
            [btn_primarina, btn_gengar],
            [btn_iron_valiant, btn_corviknight],
            [btn_view_team, btn_back]
        ]

        # 1. Verification of classification methods
        self.assertTrue(battle_handler.is_switch_prompt(switch_msg))
        self.assertFalse(battle_handler.is_battle_end(switch_msg))
        self.assertTrue(battle_handler.is_battle_message(switch_msg, "test_account"))

        # 2. Execution of turn
        mock_client = AsyncMock()
        success = await battle_handler.handle_battle_turn(mock_client, "test_account", switch_msg)
        self.assertTrue(success)

        # Primarina has 4.0x type effectiveness against Wild Arcanine-hisui (Fire/Rock)!
        btn_primarina.click.assert_called_once()
        btn_delphox.click.assert_not_called()
        btn_empty.click.assert_not_called()
        btn_view_team.click.assert_not_called()
        btn_back.click.assert_not_called()

    async def test_auto_switch_fallback_party_order(self):
        """
        When type effectiveness is equal or wild Pokémon is unknown,
        auto-switch chooses the first available alive Pokémon in party order (slot 2).
        """
        import config
        from core.pokedex import PokedexService
        from handlers.battle_handler import BattleHandler

        pokedex = PokedexService(config.POKEDEX_FILE)
        battle_handler = BattleHandler(pokedex)

        switch_msg = MagicMock()
        switch_msg.id = 5556
        switch_msg.chat_id = 999
        switch_msg.raw_text = (
            "Metagross fainted!\n\n"
            "Choose a Pokémon to switch to:\n\n"
            "1. Metagross (Fainted)\n"
            "2. Delphox\n"
            "3. Primarina\n"
            "4. Gengar"
        )

        btn_empty = MagicMock(text="", click=AsyncMock())
        btn_delphox = MagicMock(text="2", click=AsyncMock())
        btn_primarina = MagicMock(text="3", click=AsyncMock())
        btn_gengar = MagicMock(text="4", click=AsyncMock())
        btn_back = MagicMock(text="Back", click=AsyncMock())

        switch_msg.buttons = [
            [btn_empty, btn_delphox],
            [btn_primarina, btn_gengar],
            [btn_back]
        ]

        mock_client = AsyncMock()
        success = await battle_handler.handle_battle_turn(mock_client, "test_account", switch_msg)
        self.assertTrue(success)

        # Slot 2 (Delphox) must be picked as the first alive party member
        btn_delphox.click.assert_called_once()
        btn_primarina.click.assert_not_called()
        btn_empty.click.assert_not_called()
        btn_back.click.assert_not_called()

    async def test_auto_switch_disabled_setting(self):
        """
        When auto_switch is set to False, handle_battle_turn must NOT click switch buttons.
        """
        import config
        from core.pokedex import PokedexService
        from handlers.battle_handler import BattleHandler

        pokedex = PokedexService(config.POKEDEX_FILE)
        battle_handler = BattleHandler(pokedex, auto_switch=False)

        switch_msg = MagicMock()
        switch_msg.id = 5557
        switch_msg.chat_id = 999
        switch_msg.raw_text = (
            "Metagross fainted!\n"
            "Choose a Pokémon to switch to:\n"
            "1. Metagross (Fainted)\n"
            "2. Delphox"
        )
        btn_delphox = MagicMock(text="2", click=AsyncMock())
        switch_msg.buttons = [[btn_delphox]]

        mock_client = AsyncMock()
        success = await battle_handler.handle_battle_turn(mock_client, "test_account", switch_msg)
        self.assertFalse(success)
        btn_delphox.click.assert_not_called()

    async def test_auto_switch_with_named_buttons(self):
        """
        Tests switch when buttons display Pokémon names instead of numbers.
        """
        import config
        from core.pokedex import PokedexService
        from handlers.battle_handler import BattleHandler

        pokedex = PokedexService(config.POKEDEX_FILE)
        battle_handler = BattleHandler(pokedex)

        switch_msg = MagicMock()
        switch_msg.id = 5558
        switch_msg.chat_id = 999
        switch_msg.raw_text = (
            "Wild Geodude attacked!\n"
            "Pikachu fainted!\n\n"
            "Choose a Pokémon to switch to:\n\n"
            "1. Pikachu (Fainted)\n"
            "2. Squirtle\n"
            "3. Pidgey"
        )

        btn_squirtle = MagicMock(text="Squirtle", click=AsyncMock())
        btn_pidgey = MagicMock(text="Pidgey", click=AsyncMock())
        switch_msg.buttons = [[btn_squirtle, btn_pidgey]]

        mock_client = AsyncMock()
        success = await battle_handler.handle_battle_turn(mock_client, "test_account", switch_msg)
        self.assertTrue(success)

        # Squirtle (Water vs Rock/Ground) has 4.0x advantage!
        btn_squirtle.click.assert_called_once()
        btn_pidgey.click.assert_not_called()

    async def test_auto_switch_screenshot_iron_thorns_scenario(self):
        """
        Tests the exact screenshot scenario:
        Wild Iron Thorns broke free and used Wild Charge.
        Metagross fainted.
        Party:
        1. Metagross (Fainted)
        2. Delphox
        3. Primarina
        4. Gengar
        5. Iron Valiant
        6. Corviknight

        Buttons:
        Row 0: [   ] [ 2 ]
        Row 1: [ 3 ] [ 4 ]
        Row 2: [ 5 ] [ 6 ]
        Row 3: [ View Team ] [ Back ]
        """
        import config
        from core.pokedex import PokedexService
        from handlers.battle_handler import BattleHandler

        pokedex = PokedexService(config.POKEDEX_FILE)
        battle_handler = BattleHandler(pokedex, auto_switch=True)

        switch_msg = MagicMock()
        switch_msg.id = 7788
        switch_msg.chat_id = 12345
        switch_msg.raw_text = (
            "Aww! Wild Iron Thorns broke free!\n\n"
            "Wild Iron Thorns used Wild Charge\n"
            "It dealt 58 damage!\n"
            "Metagross fainted!\n\n"
            "Choose a Pokémon to switch to:\n\n"
            "1. Metagross (Fainted)\n"
            "2. Delphox\n"
            "3. Primarina\n"
            "4. Gengar\n"
            "5. Iron Valiant\n"
            "6. Corviknight"
        )

        btn_blank_1 = MagicMock(text=" ", click=AsyncMock())
        btn_2 = MagicMock(text="2", click=AsyncMock())
        btn_3 = MagicMock(text="3", click=AsyncMock())
        btn_4 = MagicMock(text="4", click=AsyncMock())
        btn_5 = MagicMock(text="5", click=AsyncMock())
        btn_6 = MagicMock(text="6", click=AsyncMock())
        btn_view_team = MagicMock(text="View Team", click=AsyncMock())
        btn_back = MagicMock(text="Back", click=AsyncMock())

        switch_msg.buttons = [
            [btn_blank_1, btn_2],
            [btn_3, btn_4],
            [btn_5, btn_6],
            [btn_view_team, btn_back]
        ]
        switch_msg.click = AsyncMock()

        # 1. Assert is_switch_prompt detects this message
        self.assertTrue(battle_handler.is_switch_prompt(switch_msg))

        # 2. Assert is_battle_end does NOT treat this as battle end
        self.assertFalse(battle_handler.is_battle_end(switch_msg))

        # 3. Execute auto-switch
        mock_client = AsyncMock()
        success = await battle_handler.handle_battle_turn(mock_client, "test_account", switch_msg)
        self.assertTrue(success)

        # 4. Verify blank slot 1 (fainted Metagross) was never clicked
        btn_blank_1.click.assert_not_called()
        btn_view_team.click.assert_not_called()
        btn_back.click.assert_not_called()

        # 5. Verify either Primarina (Slot 3, Water vs Rock: 2.0x) or Iron Valiant (Slot 5, Fighting vs Rock: 2.0x) was clicked
        clicked_btns = [
            b for b, name in [
                (btn_2, "2"), (btn_3, "3"), (btn_4, "4"), (btn_5, "5"), (btn_6, "6")
            ] if b.click.called
        ]
        self.assertEqual(len(clicked_btns), 1, "Exactly one alive Pokémon button must be clicked")
        # Primarina is slot 3, which is evaluated first among tied 2.0x effectiveness
        self.assertTrue(btn_3.click.called or btn_5.click.called)

    async def test_auto_switch_fallback_message_click_when_button_click_fails(self):
        """Tests that if chosen_btn.click() throws an error, message.click() fallback is invoked."""
        import config
        from core.pokedex import PokedexService
        from handlers.battle_handler import BattleHandler

        pokedex = PokedexService(config.POKEDEX_FILE)
        battle_handler = BattleHandler(pokedex, auto_switch=True)

        switch_msg = MagicMock()
        switch_msg.id = 7789
        switch_msg.chat_id = 12345
        switch_msg.raw_text = (
            "Metagross fainted!\n\n"
            "Choose a Pokémon to switch to:\n\n"
            "1. Metagross (Fainted)\n"
            "2. Delphox"
        )

        btn_2 = MagicMock(text="2", click=AsyncMock(side_effect=RuntimeError("Button click error")))
        switch_msg.buttons = [[MagicMock(text=" "), btn_2]]
        switch_msg.click = AsyncMock()

        mock_client = AsyncMock()
        success = await battle_handler.handle_battle_turn(mock_client, "test_account", switch_msg)
        self.assertTrue(success)
        btn_2.click.assert_called_once()
        switch_msg.click.assert_called_once_with(text="2")

if __name__ == "__main__":
    unittest.main()






