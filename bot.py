import asyncio
import logging
import re
import sys
from pathlib import Path
from typing import Dict, Any, Optional

from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
    PhoneNumberInvalidError,
    FloodWaitError
)

import config
from core.database import db
from core.multi_runner import multi_runner

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("autohexa.bot")

# In-memory dictionary for handling user login conversation state
# Schema: { user_id: { "step": str, "client": TelegramClient, "phone": str, "phone_code_hash": str, "account_name": str } }
LOGIN_STATES: Dict[int, Dict[str, Any]] = {}
# Track users inputting a custom bot username: { user_id: acc_id }
CUSTOM_TARGET_STATES: Dict[int, int] = {}

def get_main_menu_buttons(accounts_count: int = 0, running_count: int = 0) -> list:
    """Builds the main dashboard keyboard without emojis."""
    return [
        [
            Button.inline(f"My Accounts ({accounts_count})", b"list_accounts"),
            Button.inline("Add Account", b"add_account")
        ],
        [
            Button.inline(f"Start All ({running_count}/{accounts_count})", b"start_all"),
            Button.inline("Stop All", b"stop_all")
        ],
        [
            Button.inline("Global Stats", b"global_stats"),
            Button.inline("Help & Info", b"help_info")
        ],
        [
            Button.inline("Refresh Dashboard", b"menu_main")
        ]
    ]

async def build_main_dashboard_text(user_id: int, first_name: str) -> str:
    """Generates the rich status text for the main dashboard using pure English and Telegram HTML."""
    accounts = await db.get_user_accounts(user_id)
    running_count = sum(1 for a in accounts if multi_runner.is_account_running(a["id"]))
    stats = await db.get_user_total_stats(user_id)

    status_str = "ACTIVE" if running_count > 0 else "IDLE"

    return (
        "<b>AutoHexa Controller & Cloud Automation</b>\n\n"
        f"Welcome, <b>{first_name}</b>.\n"
        f"Cloud Status: <b>{status_str}</b>\n\n"
        "<b>Account Overview:</b>\n"
        f"• Total Accounts: <b>{len(accounts)}</b>\n"
        f"• Running Accounts: <b>{running_count} / {len(accounts)}</b>\n\n"
        "<b>Lifetime Statistics:</b>\n"
        f"• Pokemon Caught: <b>{stats.get('total_catches', 0):,}</b>\n"
        f"• Pokemon Defeated: <b>{stats.get('total_kills', 0):,}</b>\n"
        f"• Silhouette Guesses: <b>{stats.get('total_guesses', 0):,}</b>\n"
        f"• Anti-Bot Checks Solved: <b>{stats.get('total_checks', 0):,}</b>\n\n"
        "<i>Select an option from the menu below:</i>"
    )

async def cancel_login_state(user_id: int):
    """Safely cleans up any active login or custom input attempt."""
    state = LOGIN_STATES.pop(user_id, None)
    CUSTOM_TARGET_STATES.pop(user_id, None)
    if state and state.get("client"):
        try:
            await state["client"].disconnect()
        except Exception:
            pass

def create_bot() -> TelegramClient:
    """Initializes the controller bot client."""
    if not config.BOT_TOKEN:
        print("\n" + "=" * 70)
        print(" [!] ERROR: BOT_TOKEN is not configured in .env!")
        print(" [!] To run the Telegram Controller Bot:")
        print("     1. Open Telegram and search for @BotFather")
        print("     2. Send /newbot and create a new bot")
        print("     3. Copy the HTTP API token")
        print("     4. Add to .env: BOT_TOKEN=123456789:ABCDefGhIjKlMnOpQrStUvWxYz")
        print("=" * 70 + "\n")
        sys.exit(1)

    bot = TelegramClient(
        str(config.SESSIONS_DIR / "controller_bot"),
        config.API_ID,
        config.API_HASH
    )
    return bot

bot = create_bot()

# =========================================================================
# COMMAND HANDLERS
# =========================================================================

@bot.on(events.NewMessage(pattern=r"^/start$"))
async def handle_start(event):
    """Entry point /start command."""
    if not event.is_private:
        return

    user_id = event.sender_id
    user = await event.get_sender()
    first_name = user.first_name or "Trainer"
    username = user.username or ""

    # Register user in database
    is_admin = user_id in config.ADMIN_USER_IDS
    await db.register_user(user_id, username=username, first_name=first_name, is_admin=is_admin)

    # Cancel any pending login
    await cancel_login_state(user_id)

    accounts = await db.get_user_accounts(user_id)
    running_count = sum(1 for a in accounts if multi_runner.is_account_running(a["id"]))

    text = await build_main_dashboard_text(user_id, first_name)
    buttons = get_main_menu_buttons(len(accounts), running_count)
    await event.respond(text, buttons=buttons, parse_mode="html")

@bot.on(events.NewMessage(pattern=r"^/cancel$"))
async def handle_cancel(event):
    """Cancels any ongoing account addition wizard or custom input."""
    user_id = event.sender_id
    if user_id in LOGIN_STATES or user_id in CUSTOM_TARGET_STATES:
        await cancel_login_state(user_id)
        await event.respond(
            "<b>Operation Cancelled.</b>\n\nReturned to the main menu.",
            buttons=get_main_menu_buttons(),
            parse_mode="html"
        )
    else:
        await event.respond("No active operation to cancel.", parse_mode="html")

@bot.on(events.NewMessage(pattern=r"^(?:/|\.)go(?:@\w+)?(?:\s+.*)?$"))
async def handle_bot_go_command(event):
    """Enables auto-guess in this group for the user's accounts."""
    if event.is_private:
        await event.respond(
            "<b>/go is used in groups!</b>\n\n"
            "Add me and your userbot account to a Telegram group and type <code>/go</code> in the group "
            "to activate continuous auto-guessing in that group.\n\n"
            "To manage accounts, use /start.",
            parse_mode="html"
        )
        return

    user_id = event.sender_id
    chat_id = event.chat_id
    chat_title = getattr(event.chat, "title", str(chat_id)) or str(chat_id)

    accounts = await db.get_user_accounts(user_id)
    if not accounts:
        if user_id in config.ADMIN_USER_IDS:
            running_instances = list(multi_runner.instances.values())
        else:
            await event.reply("⚠️ You do not have any registered accounts in AutoHexa.\nOpen a private chat with me and use /start to add your account first!")
            return
    else:
        running_instances = [
            multi_runner.instances[a["id"]]
            for a in accounts
            if multi_runner.is_account_running(a["id"]) and a["id"] in multi_runner.instances
        ]
        if not running_instances:
            # Auto-start user's active or first account
            target_acc = next((a for a in accounts if a.get("is_active")), accounts[0])
            started = await multi_runner.start_account(target_acc["id"])
            if started and target_acc["id"] in multi_runner.instances:
                running_instances = [multi_runner.instances[target_acc["id"]]]

    if not running_instances:
        await event.reply("⚠️ No running accounts available to guess in this group.\nPlease use /start in private chat to start your account!")
        return

    activated_names = []
    for inst in running_instances:
        inst.active_group_chats.add(chat_id)
        inst.hunt_chat = str(chat_id)
        await db.update_account_settings(inst.acc_id, hunt_chat=str(chat_id))
        if inst.auto_guesser:
            inst.auto_guesser.switch_target_chat(inst.account_name, inst.client, chat_id)
        activated_names.append(inst.account_name)

    names_str = ", ".join([f"<b>{n}</b>" for n in activated_names])
    await event.reply(
        f"⚡ <b>Auto-Guess Activated!</b>\n\n"
        f"Group: <b>{chat_title}</b>\n"
        f"Account(s): {names_str}\n\n"
        f"Continuous <code>/guess</code> loop is now active in this group!",
        parse_mode="html"
    )

@bot.on(events.NewMessage(pattern=r"^(?:/|\.)stop(?:@\w+)?(?:\s+.*)?$"))
async def handle_bot_stop_command(event):
    """Stops auto-guess in this group for the user's accounts."""
    if event.is_private:
        return

    user_id = event.sender_id
    chat_id = event.chat_id

    accounts = await db.get_user_accounts(user_id)
    running_instances = [
        multi_runner.instances[a["id"]]
        for a in accounts
        if multi_runner.is_account_running(a["id"]) and a["id"] in multi_runner.instances
    ] if accounts else []

    if user_id in config.ADMIN_USER_IDS and not running_instances:
        running_instances = list(multi_runner.instances.values())

    stopped_names = []
    for inst in running_instances:
        if chat_id in inst.active_group_chats:
            inst.active_group_chats.discard(chat_id)
            if inst.auto_guesser:
                inst.auto_guesser.stop_account(inst.account_name)
            stopped_names.append(inst.account_name)

    if stopped_names:
        names_str = ", ".join([f"<b>{n}</b>" for n in stopped_names])
        await event.reply(
            f"⏹ <b>Auto-Guess Stopped</b>\n\n"
            f"Group auto-guess has been stopped for: {names_str}",
            parse_mode="html"
        )

# =========================================================================
# INTERACTIVE LOGIN WIZARD (TEXT INPUT)
# =========================================================================

@bot.on(events.NewMessage(func=lambda e: e.is_private and not e.text.startswith("/")))
async def handle_login_conversation(event):
    """Processes interactive text replies during phone -> code -> 2FA login or custom target bot setting."""
    user_id = event.sender_id

    # Handle custom target bot username text entry
    if user_id in CUSTOM_TARGET_STATES:
        acc_id = CUSTOM_TARGET_STATES.pop(user_id)
        acc = await db.get_account(acc_id)
        if not acc or acc["owner_id"] != user_id:
            await event.respond("Account not found or access denied.", parse_mode="html")
            return

        clean_bot = event.text.strip().lstrip("@")
        if not re.match(r"^[A-Za-z0-9_]{3,32}$", clean_bot):
            CUSTOM_TARGET_STATES[user_id] = acc_id
            await event.respond(
                "<b>Invalid Bot Username</b>\n\n"
                "Please enter a valid Telegram bot username (3 to 32 alphanumeric characters or underscores).\n"
                "<i>Example:</i> <code>HeXamonbot</code> or <code>PokemonWorldBot</code>\n\n"
                "Send /cancel to abort.",
                parse_mode="html"
            )
            return

        await db.update_account_settings(acc_id, target_bot=clean_bot, hunt_chat=clean_bot)

        if multi_runner.is_account_running(acc_id):
            await multi_runner.stop_account(acc_id)
            await multi_runner.start_account(acc_id)

        buttons = [
            [Button.inline("Back to Account", f"acc_view_{acc_id}".encode())],
            [Button.inline("Main Menu", b"menu_main")]
        ]
        await event.respond(
            "<b>Target Bot Updated</b>\n\n"
            f"Account <b>{acc['account_name']}</b> will now target <b>@{clean_bot}</b>.\n\n"
            "If the account was active, it has been restarted to monitor the new target bot.",
            buttons=buttons,
            parse_mode="html"
        )
        return

    if user_id not in LOGIN_STATES:
        return

    state = LOGIN_STATES[user_id]
    step = state.get("step")
    text = event.text.strip()

    # Step 1: Receiving Phone Number
    if step == "WAIT_PHONE":
        clean_phone = re.sub(r"[\s\-]", "", text)
        if not re.match(r"^\+\d{7,15}$", clean_phone):
            await event.respond(
                "<b>Invalid Phone Number</b>\n\n"
                "Please enter a valid phone number with country code.\n"
                "<i>Example:</i> <code>+919876543210</code>\n\n"
                "Send /cancel to abort.",
                parse_mode="html"
            )
            return

        msg = await event.respond("Connecting to Telegram servers...", parse_mode="html")
        try:
            temp_client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
            await temp_client.connect()
            sent_req = await temp_client.send_code_request(clean_phone)

            state["client"] = temp_client
            state["phone"] = clean_phone
            state["phone_code_hash"] = sent_req.phone_code_hash
            state["step"] = "WAIT_CODE"

            await msg.edit(
                "<b>Verification Code Sent</b>\n\n"
                f"A 5-digit verification code has been sent to <code>{clean_phone}</code>.\n\n"
                "<b>Important:</b> Telegram blocks raw code numbers in chat. Please format the code with <b>spaces between digits</b>:\n\n"
                "<code>1 2 3 4 5</code>\n\n"
                "<i>Send /cancel to abort.</i>",
                parse_mode="html"
            )
        except PhoneNumberInvalidError:
            await msg.edit("Phone number is not registered on Telegram or is invalid. Send /cancel to try again.", parse_mode="html")
            await cancel_login_state(user_id)
        except FloodWaitError as e:
            await msg.edit(f"Telegram FloodWait: Please retry after {e.seconds} seconds.", parse_mode="html")
            await cancel_login_state(user_id)
        except Exception as e:
            logger.error("Error during send_code_request for %s: %s", clean_phone, e)
            await msg.edit(f"Error: {e}\n\nSend /cancel to try again.", parse_mode="html")
            await cancel_login_state(user_id)
        return

    # Step 2: Receiving Telegram OTP
    if step == "WAIT_CODE":
        clean_code = re.sub(r"[\s\-]", "", text)
        temp_client: TelegramClient = state["client"]
        phone = state["phone"]
        phone_code_hash = state["phone_code_hash"]

        msg = await event.respond("Verifying login code...", parse_mode="html")
        try:
            await temp_client.sign_in(phone=phone, code=clean_code, phone_code_hash=phone_code_hash)
            # If sign_in succeeded without 2FA:
            await complete_account_login(user_id, state, event, msg)
        except SessionPasswordNeededError:
            state["step"] = "WAIT_2FA"
            await msg.edit(
                "<b>Two-Step Verification (2FA) Required</b>\n\n"
                "Two-Step Verification is enabled on this account.\n"
                "Please enter your <b>2FA Password</b>:\n\n"
                "<i>Send /cancel to abort.</i>",
                parse_mode="html"
            )
        except PhoneCodeInvalidError:
            await msg.edit(
                "<b>Invalid Verification Code</b>\n\n"
                "Please enter the correct code with spaces between digits:\n"
                "<code>1 2 3 4 5</code>",
                parse_mode="html"
            )
        except PhoneCodeExpiredError:
            await msg.edit("Verification code has expired. Please restart the process using /start.", parse_mode="html")
            await cancel_login_state(user_id)
        except Exception as e:
            logger.error("Error during sign_in for %s: %s", phone, e)
            await msg.edit(f"Login failed: {e}\n\nPlease retry or send /cancel.", parse_mode="html")
        return

    # Step 3: Receiving 2FA Password
    if step == "WAIT_2FA":
        temp_client: TelegramClient = state["client"]
        password = text
        msg = await event.respond("Verifying 2FA password...", parse_mode="html")
        try:
            await temp_client.sign_in(password=password)
            await complete_account_login(user_id, state, event, msg)
        except PasswordHashInvalidError:
            await msg.edit("<b>Invalid 2FA Password.</b> Please enter the correct password:", parse_mode="html")
        except Exception as e:
            logger.error("Error during 2FA for %s: %s", state.get("phone"), e)
            await msg.edit(f"2FA verification failed: {e}\n\nPlease retry or send /cancel.", parse_mode="html")
        return

async def complete_account_login(user_id: int, state: dict, event, msg_to_edit):
    """Finalizes successful login, saves session in DB, and shows confirmation."""
    temp_client: TelegramClient = state["client"]
    phone = state["phone"]
    me = await temp_client.get_me()

    session_string = temp_client.session.save()
    await temp_client.disconnect()

    account_name = f"@{me.username}" if me.username else (me.first_name or f"Acc_{phone[-4:]}")

    # Add account to database
    acc_id = await db.add_account(
        owner_id=user_id,
        account_name=account_name,
        phone=phone,
        session_string=session_string,
        action_mode=config.ACTION_MODE,
        hunt_mode=config.HUNT_MODE,
        target_bot=config.TARGET_BOT,
        hunt_chat=config.HUNT_CHAT
    )

    # Clean up state
    LOGIN_STATES.pop(user_id, None)

    buttons = [
        [
            Button.inline("Start This Account", f"start_acc_{acc_id}".encode()),
            Button.inline("Settings", f"acc_view_{acc_id}".encode())
        ],
        [
            Button.inline("My Accounts", b"list_accounts"),
            Button.inline("Main Menu", b"menu_main")
        ]
    ]

    await msg_to_edit.edit(
        "<b>Account Added Successfully</b>\n\n"
        f"<b>Account:</b> {account_name}\n"
        f"<b>Phone:</b> <code>{phone}</code>\n"
        f"<b>Account ID:</b> #{acc_id}\n"
        f"<b>Target:</b> @{config.TARGET_BOT}\n"
        f"<b>Default Mode:</b> {config.ACTION_MODE.upper()}\n\n"
        "Your session has been saved securely in the database.\n"
        "Click the <b>Start This Account</b> button below to begin automated farming.",
        buttons=buttons,
        parse_mode="html"
    )

# =========================================================================
# INLINE CALLBACK QUERY HANDLERS
# =========================================================================

@bot.on(events.CallbackQuery)
async def handle_callback_queries(event, custom_data: Optional[bytes] = None):
    """Dispatches all inline button interactions cleanly."""
    raw_data = custom_data if custom_data is not None else event.data
    data = raw_data.decode("utf-8") if isinstance(raw_data, bytes) else str(raw_data)
    user_id = event.sender_id
    user = await event.get_sender()
    first_name = user.first_name if (user and user.first_name) else "Trainer"

    # Dismiss any active custom target bot input state if user navigates via buttons
    if not data.startswith("set_custom_target_"):
        CUSTOM_TARGET_STATES.pop(user_id, None)

    # --- 1. MAIN MENU ---
    if data == "menu_main":
        accounts = await db.get_user_accounts(user_id)
        running_count = sum(1 for a in accounts if multi_runner.is_account_running(a["id"]))
        text = await build_main_dashboard_text(user_id, first_name)
        buttons = get_main_menu_buttons(len(accounts), running_count)
        await event.edit(text, buttons=buttons, parse_mode="html")
        await event.answer()
        return

    # --- 2. ADD ACCOUNT WIZARD ---
    if data == "add_account":
        await cancel_login_state(user_id)
        LOGIN_STATES[user_id] = {"step": "WAIT_PHONE"}

        text = (
            "<b>Add New Telegram Account</b>\n\n"
            "Please send your phone number with the country code:\n\n"
            "<i>Example:</i> <code>+919876543210</code>\n\n"
            "Send /cancel at any time to abort."
        )
        buttons = [[Button.inline("Back to Main Menu", b"menu_main")]]
        await event.edit(text, buttons=buttons, parse_mode="html")
        await event.answer()
        return

    # --- 3. LIST ACCOUNTS ---
    if data == "list_accounts":
        accounts = await db.get_user_accounts(user_id)
        if not accounts:
            text = (
                "<b>My Accounts</b>\n\n"
                "You do not have any connected accounts yet.\n"
                "Use the button below to add your first account."
            )
            buttons = [
                [Button.inline("Add Account", b"add_account")],
                [Button.inline("Main Menu", b"menu_main")]
            ]
            await event.edit(text, buttons=buttons, parse_mode="html")
            await event.answer()
            return

        text = f"<b>Connected Accounts ({len(accounts)}):</b>\n\nSelect an account to view controls and statistics:"
        buttons = []
        for acc in accounts:
            is_run = multi_runner.is_account_running(acc["id"])
            status_tag = "[RUNNING]" if is_run else "[STOPPED]"
            btn_text = f"{status_tag} {acc['account_name']} ({acc['phone']})"
            buttons.append([Button.inline(btn_text, f"acc_view_{acc['id']}".encode())])

        buttons.append([
            Button.inline("Add Account", b"add_account"),
            Button.inline("Main Menu", b"menu_main")
        ])
        await event.edit(text, buttons=buttons, parse_mode="html")
        await event.answer()
        return

    # --- 4. VIEW SINGLE ACCOUNT ---
    if data.startswith("acc_view_"):
        acc_id = int(data.split("_")[-1])
        acc = await db.get_account(acc_id)
        if not acc or acc["owner_id"] != user_id:
            await event.answer("Account not found.", alert=True)
            return

        is_run = multi_runner.is_account_running(acc_id)
        stats = await db.get_stats(acc_id)
        status_text = "RUNNING" if is_run else "STOPPED"

        card_text = (
            f"<b>Account Details:</b> #{acc['id']}\n\n"
            f"<b>Name:</b> {acc['account_name']}\n"
            f"<b>Phone:</b> <code>{acc['phone']}</code>\n"
            f"<b>Status:</b> <b>{status_text}</b>\n\n"
            "<b>Configuration:</b>\n"
            f"• Action Mode: <b>{acc.get('action_mode', 'guess').upper()}</b>\n"
            f"• Hunt Strategy: <b>{acc.get('hunt_mode', 'catch').upper()}</b>\n"
            f"• Auto Switch: <b>{'ON' if acc.get('auto_switch', 1) else 'OFF'}</b>\n"
            f"• Target Bot: @{acc.get('target_bot', 'HeXamonbot')}\n\n"
            "<b>Performance Statistics:</b>\n"
            f"• Catches: <b>{stats.get('catches', 0):,}</b>\n"
            f"• Kills: <b>{stats.get('kills', 0):,}</b>\n"
            f"• Guesses: <b>{stats.get('guesses', 0):,}</b>\n"
            f"• Checks Solved: <b>{stats.get('checks_solved', 0):,}</b>"
        )

        toggle_btn = (
            Button.inline("Stop Auto", f"stop_acc_{acc_id}".encode())
            if is_run
            else Button.inline("Start Auto", f"start_acc_{acc_id}".encode())
        )

        buttons = [
            [toggle_btn],
            [
                Button.inline(f"Mode: {acc.get('action_mode', 'guess').upper()}", f"toggle_action_{acc_id}".encode()),
                Button.inline(f"Strategy: {acc.get('hunt_mode', 'catch').upper()}", f"toggle_hunt_{acc_id}".encode())
            ],
            [
                Button.inline(f"Auto Switch: {'ON' if acc.get('auto_switch', 1) else 'OFF'}", f"toggle_switch_{acc_id}".encode()),
                Button.inline(f"Target Bot: @{acc.get('target_bot', 'HeXamonbot')}", f"target_menu_{acc_id}".encode())
            ],
            [
                Button.inline("Remove Account", f"del_confirm_{acc_id}".encode())
            ],
            [
                Button.inline("Back to Accounts", b"list_accounts"),
                Button.inline("Main Menu", b"menu_main")
            ]
        ]
        await event.edit(card_text, buttons=buttons, parse_mode="html")
        await event.answer()
        return

    # --- 5. START / STOP SINGLE ACCOUNT ---
    if data.startswith("start_acc_"):
        acc_id = int(data.split("_")[-1])
        acc = await db.get_account(acc_id)
        if not acc or acc["owner_id"] != user_id:
            await event.answer("Unauthorized.", alert=True)
            return

        await event.answer("Starting account...", alert=False)
        success = await multi_runner.start_account(acc_id)
        if success:
            await event.answer("Account is now active and running.", alert=True)
        else:
            await event.answer("Failed to start account. Check session validity or rate limits.", alert=True)

        # Refresh account card
        await handle_callback_queries(event, custom_data=f"acc_view_{acc_id}".encode())
        return

    if data.startswith("stop_acc_"):
        acc_id = int(data.split("_")[-1])
        acc = await db.get_account(acc_id)
        if not acc or acc["owner_id"] != user_id:
            await event.answer("Unauthorized.", alert=True)
            return

        await event.answer("Stopping account...", alert=False)
        await multi_runner.stop_account(acc_id)
        await event.answer("Account has been stopped.", alert=True)

        # Refresh account card
        await handle_callback_queries(event, custom_data=f"acc_view_{acc_id}".encode())
        return

    # --- 6. SETTINGS TOGGLES ---
    if data.startswith("toggle_action_"):
        acc_id = int(data.split("_")[-1])
        acc = await db.get_account(acc_id)
        if not acc or acc["owner_id"] != user_id:
            await event.answer("Unauthorized.", alert=True)
            return

        modes = ["guess", "hunt", "both"]
        current = acc.get("action_mode", "guess")
        next_mode = modes[(modes.index(current) + 1) % len(modes)] if current in modes else "guess"
        await db.update_account_settings(acc_id, action_mode=next_mode)

        # If currently running, restart to apply new mode immediately
        if multi_runner.is_account_running(acc_id):
            await multi_runner.stop_account(acc_id)
            await multi_runner.start_account(acc_id)

        await event.answer(f"Action mode updated to: {next_mode.upper()}")
        await handle_callback_queries(event, custom_data=f"acc_view_{acc_id}".encode())
        return

    if data.startswith("toggle_hunt_"):
        acc_id = int(data.split("_")[-1])
        acc = await db.get_account(acc_id)
        if not acc or acc["owner_id"] != user_id:
            await event.answer("Unauthorized.", alert=True)
            return

        current = acc.get("hunt_mode", "catch")
        next_mode = "kill" if current == "catch" else "catch"
        await db.update_account_settings(acc_id, hunt_mode=next_mode)

        # If currently running, update live instance and its handlers immediately
        if acc_id in multi_runner.instances:
            multi_runner.instances[acc_id].set_hunt_mode(next_mode)

        await event.answer(f"Hunt strategy updated to: {next_mode.upper()}")
        await handle_callback_queries(event, custom_data=f"acc_view_{acc_id}".encode())
        return

    if data.startswith("toggle_switch_"):
        acc_id = int(data.split("_")[-1])
        acc = await db.get_account(acc_id)
        if not acc or acc["owner_id"] != user_id:
            await event.answer("Unauthorized.", alert=True)
            return

        current = acc.get("auto_switch", 1)
        next_val = 0 if current else 1
        await db.update_account_settings(acc_id, auto_switch=next_val)

        if acc_id in multi_runner.instances:
            multi_runner.instances[acc_id].set_auto_switch(bool(next_val))

        state_str = "ENABLED" if next_val else "DISABLED"
        await event.answer(f"Auto Switch is now: {state_str}")
        await handle_callback_queries(event, custom_data=f"acc_view_{acc_id}".encode())
        return

    # --- 7. TARGET BOT MENU AND SELECTION ---
    if data.startswith("target_menu_"):
        acc_id = int(data.split("_")[-1])
        acc = await db.get_account(acc_id)
        if not acc or acc["owner_id"] != user_id:
            await event.answer("Account not found.", alert=True)
            return

        curr_bot = acc.get("target_bot", "HeXamonbot")
        text = (
            "<b>Select Target Pokemon Bot</b>\n\n"
            f"Account: <b>{acc['account_name']}</b>\n"
            f"Current Target: <b>@{curr_bot}</b>\n\n"
            "Choose a preset bot below, or select <b>Custom Bot Username</b> to enter any Telegram bot:"
        )

        buttons = [
            [
                Button.inline("HeXamonbot (@HeXamonbot)", f"set_target_{acc_id}_HeXamonbot".encode())
            ],
            [
                Button.inline("PokemonWorldBot (@PokemonWorldBot)", f"set_target_{acc_id}_PokemonWorldBot".encode())
            ],
            [
                Button.inline("Custom Bot Username", f"set_custom_target_{acc_id}".encode())
            ],
            [
                Button.inline("Back to Account", f"acc_view_{acc_id}".encode())
            ]
        ]
        await event.edit(text, buttons=buttons, parse_mode="html")
        await event.answer()
        return

    if data.startswith("set_target_"):
        parts = data.split("_")
        acc_id = int(parts[2])
        chosen_bot = parts[3]
        acc = await db.get_account(acc_id)
        if not acc or acc["owner_id"] != user_id:
            await event.answer("Unauthorized.", alert=True)
            return

        await db.update_account_settings(acc_id, target_bot=chosen_bot, hunt_chat=chosen_bot)

        if multi_runner.is_account_running(acc_id):
            await multi_runner.stop_account(acc_id)
            await multi_runner.start_account(acc_id)

        await event.answer(f"Target bot set to @{chosen_bot}", alert=True)
        await handle_callback_queries(event, custom_data=f"acc_view_{acc_id}".encode())
        return

    if data.startswith("set_custom_target_"):
        acc_id = int(data.split("_")[-1])
        acc = await db.get_account(acc_id)
        if not acc or acc["owner_id"] != user_id:
            await event.answer("Unauthorized.", alert=True)
            return

        CUSTOM_TARGET_STATES[user_id] = acc_id

        text = (
            "<b>Set Custom Target Bot</b>\n\n"
            f"Account: <b>{acc['account_name']}</b>\n"
            f"Current Target: <b>@{acc.get('target_bot', 'HeXamonbot')}</b>\n\n"
            "Please send the Telegram username of the bot you want this account to target.\n\n"
            "<i>Example:</i> <code>HeXamonbot</code> or <code>PokemonWorldBot</code>\n\n"
            "Send /cancel to abort."
        )
        buttons = [[Button.inline("Cancel", f"acc_view_{acc_id}".encode())]]
        await event.edit(text, buttons=buttons, parse_mode="html")
        await event.answer()
        return

    # --- 8. DELETE ACCOUNT CONFIRMATION ---
    if data.startswith("del_confirm_"):
        acc_id = int(data.split("_")[-1])
        acc = await db.get_account(acc_id)
        if not acc or acc["owner_id"] != user_id:
            await event.answer("Unauthorized.", alert=True)
            return

        text = (
            "<b>Confirm Account Deletion</b>\n\n"
            f"Are you sure you want to remove <b>{acc['account_name']}</b> (<code>{acc['phone']}</code>)?\n\n"
            "This will delete the stored session and stop all automation for this account."
        )
        buttons = [
            [
                Button.inline("Confirm Deletion", f"del_exec_{acc_id}".encode()),
                Button.inline("Cancel", f"acc_view_{acc_id}".encode())
            ]
        ]
        await event.edit(text, buttons=buttons, parse_mode="html")
        await event.answer()
        return

    if data.startswith("del_exec_"):
        acc_id = int(data.split("_")[-1])
        # Stop first
        await multi_runner.stop_account(acc_id)
        # Delete from DB
        deleted = await db.delete_account(acc_id, user_id)
        if deleted:
            await event.answer("Account permanently deleted.", alert=True)
        else:
            await event.answer("Failed to delete account.", alert=True)

        await handle_callback_queries(event, custom_data=b"list_accounts")
        return

    # --- 8. GLOBAL START ALL / STOP ALL ---
    if data == "start_all":
        accounts = await db.get_user_accounts(user_id)
        if not accounts:
            await event.answer("No accounts available. Please add an account first.", alert=True)
            return

        await event.answer("Starting all accounts...", alert=False)
        started = await multi_runner.start_all_for_user(user_id)
        await event.answer(f"{started} accounts started.", alert=True)

        await handle_callback_queries(event, custom_data=b"menu_main")
        return

    if data == "stop_all":
        await event.answer("Stopping all accounts...", alert=False)
        stopped = await multi_runner.stop_all_for_user(user_id)
        await event.answer(f"{stopped} accounts stopped.", alert=True)

        await handle_callback_queries(event, custom_data=b"menu_main")
        return

    # --- 9. GLOBAL STATS ---
    if data == "global_stats":
        stats = await db.get_user_total_stats(user_id)
        accounts = await db.get_user_accounts(user_id)

        text = (
            "<b>Detailed Statistics</b>\n\n"
            f"<b>Trainer:</b> {first_name}\n"
            f"<b>Total Accounts:</b> {len(accounts)}\n\n"
            "<b>Aggregated Achievements:</b>\n"
            f"• Pokemon Caught: <b>{stats.get('total_catches', 0):,}</b>\n"
            f"• Pokemon Defeated: <b>{stats.get('total_kills', 0):,}</b>\n"
            f"• Silhouette Guesses: <b>{stats.get('total_guesses', 0):,}</b>\n"
            f"• Anti-Bot Checks Solved: <b>{stats.get('total_checks', 0):,}</b>\n"
            f"• Total PD Farmed: <b>{stats.get('total_pd', 0):,}</b>\n\n"
            "<i>Updated in real-time with game events.</i>"
        )
        buttons = [
            [Button.inline("Refresh Stats", b"global_stats")],
            [Button.inline("Back to Main Menu", b"menu_main")]
        ]
        await event.edit(text, buttons=buttons, parse_mode="html")
        await event.answer()
        return

    # --- 10. HELP & INFO ---
    if data == "help_info":
        text = (
            "<b>AutoHexa Controller Guide</b>\n\n"
            "<b>1. How to Add an Account:</b>\n"
            "• Click 'Add Account'.\n"
            "• Send your phone number with country code (e.g. <code>+919876543210</code>).\n"
            "• Send the Telegram login code formatted with <b>spaces between digits</b> (e.g. <code>1 2 3 4 5</code>).\n"
            "• If Two-Step Verification is enabled, enter your password.\n\n"
            "<b>2. Operating Modes:</b>\n"
            "• <b>GUESS:</b> Plays the /guess silhouette game to continuously farm Pokédollars.\n"
            "• <b>HUNT:</b> Performs /hunt commands to battle and catch wild Pokémon.\n"
            "• <b>BOTH:</b> Executes both hunting and guessing loops concurrently.\n\n"
            "<b>3. Anti-Bot Safety:</b>\n"
            "• Captchas and trivia verification questions are solved using Gemini AI and the local database.\n"
            "• If a check cannot be verified with high confidence, automation is paused and an alert is dispatched."
        )
        buttons = [[Button.inline("Back to Main Menu", b"menu_main")]]
        await event.edit(text, buttons=buttons, parse_mode="html")
        await event.answer()
        return

# =========================================================================
# MAIN SERVER RUNNER
# =========================================================================

async def start_healthcheck_server():
    """Optional lightweight HTTP server to satisfy Railway / PaaS $PORT healthchecks."""
    import os
    port_str = os.getenv("PORT")
    if not port_str:
        return
    try:
        from aiohttp import web
        port = int(port_str)
        app = web.Application()

        async def health(request):
            return web.json_response({
                "status": "healthy",
                "service": "AutoHexa Controller Bot",
                "active_accounts": len(multi_runner.instances)
            })

        app.router.add_get("/", health)
        app.router.add_get("/health", health)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info("✓ Cloud healthcheck HTTP server listening on 0.0.0.0:%d", port)
    except Exception as e:
        logger.warning("Could not start HTTP healthcheck server on port %s: %s", port_str, e)

async def run_controller_bot():
    """Main startup lifecycle for the bot and background engine."""
    logger.info("Initializing AutoHexa Controller Bot...")

    # Start optional Railway $PORT healthcheck
    await start_healthcheck_server()

    await bot.start(bot_token=config.BOT_TOKEN)
    me = await bot.get_me()
    logger.info("✓ Controller Bot ONLINE as @%s (ID: %d)", me.username, me.id)

    # Link bot client with multi-runner for user alerts
    multi_runner.set_bot_client(bot)

    # Auto-resume previously running accounts
    await multi_runner.resume_all_active()

    logger.info("=" * 60)
    logger.info(" AutoHexa Telegram Controller SaaS is Ready!")
    logger.info(" Users can now interact with @%s to add and control accounts.", me.username)
    logger.info("=" * 60)

    # Keep running
    await bot.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(run_controller_bot())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user or system signal.")
