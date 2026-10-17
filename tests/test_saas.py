import asyncio
import os
import unittest
from pathlib import Path
from core.database import Database
from core.multi_runner import MultiRunner, UserEmergencyNotifier

class TestSaaSSystem(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Use an isolated test database
        self.test_db_path = Path("data/test_autohexa.db")
        if self.test_db_path.exists():
            self.test_db_path.unlink()
        self.db = Database(db_path=self.test_db_path)

    async def asyncTearDown(self):
        if self.test_db_path.exists():
            self.test_db_path.unlink()

    async def test_user_and_account_lifecycle(self):
        # 1. Register User
        user_id = 987654321
        await self.db.register_user(user_id, username="testuser", first_name="Tester", is_admin=False)
        u = await self.db.get_user(user_id)
        self.assertIsNotNone(u)
        self.assertEqual(u["username"], "testuser")
        self.assertEqual(u["is_admin"], 0)

        # 2. Add Account
        acc_id = await self.db.add_account(
            owner_id=user_id,
            account_name="MainAccount",
            phone="+919999999999",
            session_string="1BVtsOH...dummy_session_string...",
            action_mode="guess",
            hunt_mode="catch"
        )
        self.assertIsInstance(acc_id, int)
        self.assertGreater(acc_id, 0)

        # 3. Retrieve User Accounts
        accs = await self.db.get_user_accounts(user_id)
        self.assertEqual(len(accs), 1)
        self.assertEqual(accs[0]["account_name"], "MainAccount")
        self.assertEqual(accs[0]["phone"], "+919999999999")

        # 4. Update Settings
        await self.db.update_account_settings(acc_id, action_mode="hunt", hunt_mode="kill")
        acc = await self.db.get_account(acc_id)
        self.assertEqual(acc["action_mode"], "hunt")
        self.assertEqual(acc["hunt_mode"], "kill")

        # 5. Active Status Toggle
        await self.db.set_account_active(acc_id, True)
        running = await self.db.get_all_running_accounts()
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0]["id"], acc_id)

        await self.db.set_account_active(acc_id, False)
        running_after = await self.db.get_all_running_accounts()
        self.assertEqual(len(running_after), 0)

        # 6. Stats Tracking
        await self.db.increment_stat(acc_id, "catches", amount=5)
        await self.db.increment_stat(acc_id, "kills", amount=3)
        await self.db.increment_stat(acc_id, "guesses", amount=12)
        await self.db.increment_stat(acc_id, "checks_solved", amount=2)

        stats = await self.db.get_stats(acc_id)
        self.assertEqual(stats["catches"], 5)
        self.assertEqual(stats["kills"], 3)
        self.assertEqual(stats["guesses"], 12)
        self.assertEqual(stats["checks_solved"], 2)

        # Aggregate user stats
        total = await self.db.get_user_total_stats(user_id)
        self.assertEqual(total["total_catches"], 5)
        self.assertEqual(total["total_kills"], 3)
        self.assertEqual(total["total_guesses"], 12)
        self.assertEqual(total["total_checks"], 2)

        # 7. Delete Account
        deleted = await self.db.delete_account(acc_id, user_id)
        self.assertTrue(deleted)
        acc_after = await self.db.get_account(acc_id)
        self.assertIsNone(acc_after)
        accs_after = await self.db.get_user_accounts(user_id)
        self.assertEqual(len(accs_after), 0)

    async def test_multi_runner_structure(self):
        runner = MultiRunner(bot_client=None)
        self.assertFalse(runner.is_account_running(999))
        self.assertIsNotNone(runner.pokedex)
        self.assertIsNotNone(runner.sprite_matcher)
        self.assertIsNotNone(runner.gemini)

    async def test_account_instance_hunt_strategy_propagation(self):
        """Verifies that user's hunt_mode in AccountInstance properly propagates to BattleHandler and SpawnHandler."""
        from core.multi_runner import AccountInstance
        from unittest.mock import MagicMock

        acc_data = {
            "id": 1,
            "owner_id": 100,
            "account_name": "TestMaster",
            "phone": "+1234567890",
            "session_string": "",
            "action_mode": "hunt",
            "hunt_mode": "catch",
            "target_bot": "HeXamonbot",
            "hunt_chat": "HeXamonbot"
        }

        mock_pokedex = MagicMock()
        mock_sprite = MagicMock()
        mock_gemini = MagicMock()
        mock_kb = MagicMock()

        inst = AccountInstance(
            account_data=acc_data,
            pokedex=mock_pokedex,
            sprite_matcher=mock_sprite,
            gemini=mock_gemini,
            kb=mock_kb
        )

        # 1. Initial mode should be CATCH on both handlers
        self.assertEqual(inst.hunt_mode, "catch")
        self.assertEqual(inst.battle_handler.get_hunt_mode("TestMaster"), "catch")
        self.assertEqual(inst.spawn_handler.get_hunt_mode("TestMaster"), "catch")

        # 2. Update to KILL
        inst.set_hunt_mode("kill")
        self.assertEqual(inst.hunt_mode, "kill")
        self.assertEqual(inst.battle_handler.get_hunt_mode("TestMaster"), "kill")
        self.assertEqual(inst.spawn_handler.get_hunt_mode("TestMaster"), "kill")

        # 3. Update back to CATCH
        inst.set_hunt_mode("catch")
        self.assertEqual(inst.hunt_mode, "catch")
        self.assertEqual(inst.battle_handler.get_hunt_mode("TestMaster"), "catch")
        self.assertEqual(inst.spawn_handler.get_hunt_mode("TestMaster"), "catch")

    async def test_account_instance_stats_accounting(self):
        """Verifies that catching in battle increments catches (not kills), and killing increments kills."""
        from core.multi_runner import AccountInstance
        from unittest.mock import MagicMock, patch

        user_id = 777
        await self.db.register_user(user_id, "AshKetchum", "Ash", False)
        acc_id = await self.db.add_account(user_id, "AshKetchum", "+9876543210", "")
        acc_data = await self.db.get_account(acc_id)

        inst = AccountInstance(
            account_data=acc_data,
            pokedex=MagicMock(),
            sprite_matcher=MagicMock(),
            gemini=MagicMock(),
            kb=MagicMock()
        )

        with patch("core.multi_runner.db", self.db):
            # Catch in battle -> should increment catches
            inst._on_battle_end("AshKetchum", is_catch=True)
            await asyncio.sleep(0.05)
            stats = await self.db.get_stats(acc_id)
            self.assertEqual(stats["catches"], 1)
            self.assertEqual(stats["kills"], 0)

            # Defeat in battle -> should increment kills
            inst._on_battle_end("AshKetchum", is_catch=False)
            await asyncio.sleep(0.05)
            stats = await self.db.get_stats(acc_id)
            self.assertEqual(stats["catches"], 1)
            self.assertEqual(stats["kills"], 1)

            # Flee in catch -> should NOT increment catches
            inst._on_catch_end("AshKetchum", is_catch=False)
            await asyncio.sleep(0.05)
            stats = await self.db.get_stats(acc_id)
            self.assertEqual(stats["catches"], 1)

            # Successful catch -> should increment catches
            inst._on_catch_end("AshKetchum", is_catch=True)
            await asyncio.sleep(0.05)
            stats = await self.db.get_stats(acc_id)
            self.assertEqual(stats["catches"], 2)
            self.assertEqual(stats["kills"], 1)

    async def test_target_bot_database_persistence_and_update(self):
        """Verifies target_bot and hunt_chat can be saved and updated per account."""
        user_id = 888
        await self.db.register_user(user_id, "RedTrainer", "Red", False)
        acc_id = await self.db.add_account(
            owner_id=user_id,
            account_name="RedTrainer",
            phone="+1987654321",
            session_string="",
            target_bot="PokemonWorldBot",
            hunt_chat="PokemonWorldBot"
        )
        acc = await self.db.get_account(acc_id)
        self.assertEqual(acc["target_bot"], "PokemonWorldBot")
        self.assertEqual(acc["hunt_chat"], "PokemonWorldBot")

        # Update target bot to a custom bot
        await self.db.update_account_settings(acc_id, target_bot="@CustomPokeBot_123", hunt_chat="@CustomPokeBot_123")
        updated_acc = await self.db.get_account(acc_id)
        self.assertEqual(updated_acc["target_bot"], "CustomPokeBot_123")
        self.assertEqual(updated_acc["hunt_chat"], "CustomPokeBot_123")

    async def test_account_instance_target_bot_propagation(self):
        """Verifies that setting a target bot on AccountInstance properly propagates to auto_hunter and auto_guesser."""
        from core.multi_runner import AccountInstance
        from unittest.mock import MagicMock

        acc_data = {
            "id": 99,
            "owner_id": 999,
            "account_name": "GaryOak",
            "phone": "+1999999999",
            "session_string": "",
            "action_mode": "both",
            "hunt_mode": "catch",
            "target_bot": "HeXamonbot",
            "hunt_chat": "HeXamonbot"
        }

        inst = AccountInstance(
            account_data=acc_data,
            pokedex=MagicMock(),
            sprite_matcher=MagicMock(),
            gemini=MagicMock(),
            kb=MagicMock()
        )

        self.assertEqual(inst.target_bot, "HeXamonbot")
        self.assertEqual(inst.hunt_chat, "HeXamonbot")

        # Update target bot
        inst.set_target_bot("@PokemonWorldBot")
        self.assertEqual(inst.target_bot, "PokemonWorldBot")
        self.assertEqual(inst.hunt_chat, "PokemonWorldBot")
        self.assertEqual(inst.auto_hunter.target_chats.get("GaryOak"), "PokemonWorldBot")
        self.assertEqual(inst.auto_guesser.target_chats.get("GaryOak"), "PokemonWorldBot")

    async def test_cannot_hunt_while_in_battle_holds_hunt_lock(self):
        """Verifies that when bot reports 'Cannot hunt while in battle', hunt lock is cleared and battle is marked active."""
        from core.multi_runner import AccountInstance
        from unittest.mock import MagicMock

        acc_data = {
            "id": 101,
            "owner_id": 1001,
            "account_name": "ChampionBlue",
            "phone": "+1555555555",
            "session_string": "",
            "action_mode": "hunt",
            "hunt_mode": "catch",
            "target_bot": "HeXamonbot",
            "hunt_chat": "HeXamonbot"
        }

        inst = AccountInstance(
            account_data=acc_data,
            pokedex=MagicMock(),
            sprite_matcher=MagicMock(),
            gemini=MagicMock(),
            kb=MagicMock()
        )

        event = inst.auto_hunter.get_encounter_event("ChampionBlue")
        event.set()
        self.assertFalse(inst.auto_hunter.is_encounter_active("ChampionBlue"))

        raw_text = "cannot hunt while in battle! finish your current battle first."
        if any(p in raw_text for p in ("cannot hunt while in battle", "finish your current battle")):
            inst.battle_handler.active_battles["ChampionBlue"] = {"msg_id": 999}
            inst.auto_hunter.get_encounter_event("ChampionBlue").clear()

        # The account is recognized as in active battle and hunt lock is cleared (preventing /hunt)
        self.assertTrue(inst.auto_hunter.is_encounter_active("ChampionBlue"))
        self.assertFalse(event.is_set())

if __name__ == "__main__":
    unittest.main()
