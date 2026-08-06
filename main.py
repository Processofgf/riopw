import logging
import os
import re
import random
import string
import asyncio
from datetime import datetime, timezone, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from pyrogram.errors import SessionPasswordNeeded

from creator import (
    create_escrow_group,
    start_userbot,
    stop_userbot,
    userbot_session_exists,
    userbot_is_running,
    login_send_code,
    login_sign_in,
    login_check_password,
    login_finish,
    UserbotLeaveFailed,
    cleanup_group,
)
from deal_image import generate_deal_image

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
BOT_USERNAME = "pagalescowbot"
OWNER_ID = 5697054139  # sirf ye user /addbot chala sakta hai

UPDATES_LINK = "https://t.me/BSR_ShoppiE"
VOUCHES_LINK = "https://t.me/PagaL_Escrow_Vouches"
# ===========================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# in-memory store. Production me isko DB (mongo/sqlite) me shift karna,
# restart hote hi ye khaali ho jayega
# format: {chat_id: {"members": set(user_ids), "max_members": 2}}
escrow_groups = {}

# jo groups me userbot leave nahi kar payi (unhe user ko fail bata diya
# gaya hai) - background job time-to-time inhe retry/delete karta rahega
failed_groups = set()

# ---------------- Referral / invite system (in-memory) ----------------
known_users = set()          # sab user_ids jinhone kabhi bhi /start kiya
referral_codes = {}          # code -> referrer_user_id
referred_users = {}          # invitee_user_id -> referrer_user_id (double-count na ho isliye)
referrals = {}               # user_id -> {"total_invites": int, "tickets": int}


def generate_referral_code(user_id: int) -> str:
    """Har user ke liye ek consistent (deterministic) 15-char alphanumeric code."""
    rng = random.Random(user_id)
    charset = string.ascii_uppercase + string.digits
    return "".join(rng.choice(charset) for _ in range(15))


def get_or_create_referral(user_id: int) -> dict:
    return referrals.setdefault(user_id, {"total_invites": 0, "tickets": 0})


def build_invite_text(user_id: int) -> str:
    code = generate_referral_code(user_id)
    referral_codes[code] = user_id  # reverse-lookup ke liye register kar do
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{code}"
    data = get_or_create_referral(user_id)

    return (
        f"📍 <b>Total Invites:</b> {data['total_invites']} 👤\n"
        f"📍 <b>Tickets:</b> {data['tickets']} 🎫\n\n"
        "💡 <b>Note:</b> Each voucher equals 25.0% off on fees!\n\n"
        "⚡ For every <b>new user</b> you invite, you get <b>2 fee tickets</b>.\n"
        "⚡ For every <b>old user</b> (who has already interacted with the bot), you get "
        "<b>1 fee tickets</b>, you can invite them via your referral link too—"
        "<b>for the first time</b>! Yes, you heard it right! We value your previous "
        "invites and reward you for them as well.\n\n"
        "Send the link below to users and earn <b>fee reduction tickets</b> for free "
        "once they complete minimum $1 worth of Escrows.\n\n"
        f"<b>Your Invite Link:</b>\n{link}\n\n"
        "Start sharing and enjoy CRAZY fee discounts! 🎉"
    )

# ---------------- Static text blocks ----------------
COMMANDS_TEXT = (
    "📌 <b>AVAILABLE COMMANDS</b>\n\n"
    "Here you have a full command list, incase you do like to move through the bot using commands instead of the buttons.\n\n"
    "/start - A command to start interacting with the bot\n"
    "/whatisescrow - A command to tell you more about escrow\n"
    "/instructions - A command with text instructions\n"
    "/terms - A command to bring out our TOS\n"
    "/dispute - A command to contact the admins\n"
    "/menu - A command to bring out a menu for the bot\n"
    "/contact - A command to get admin's contact\n"
    "/commands - A command to get commands list\n"
    "/stats - A command to check user stats\n"
    "/vouch - A command to vouch for the bot\n"
    "/newdeal - A command to start a new deal\n"
    "/tradeid - A command to get trade id for a chat\n"
    "/dd - A command to add deal details\n"
    "/escrow - A command to get a escrow group link\n"
    "/token - A command to select token for the escrow\n"
    "/deposit - A command to generate deposit address\n"
    "/verify - A command to verify wallet address.\n"
    "/dispute - A command to raise a dispute request\n"
    "/balance - A command to check the balance of the escrow address\n"
    "/release - A command to release the funds in the escrow\n"
    "/refund - A command to refund the funds in the escrow\n"
    "/seller - A command to set the seller\n"
    "/buyer - A command to set the buyer\n"
    "/setfee - A command to set custom trade fee\n"
    "/save - A command to save default addresses for various chains.\n"
    "/saved - A command to check saved addresses\n"
    "/referral - A command to check your referrals"
)

INSTRUCTIONS_TEXT = (
    "📘 <b>GUIDE \u201c HOW TO USE @{bot} ( Escrow Bot ) \u201c FOR SAFE AND FASTEST HASSLE-FREE ESCROW</b> 🚀\n\n"
    "<b>Step 1</b> : Use /escrow command in the DM of the Bot.\n"
    "( It will auto-create a safe escrow group and drop the link so that buyer and seller can join via that link. ) 🔗👥\n\n"
    "<b>Step 2</b> : Use /dd command to initiate the process of escrow where you will get the format to express your deal and info.\n"
    "( It will include quantity, rate, TnC's agreed upon by both parties. ) 📝🤝\n\n"
    "<b>Step 3</b> : Use /buyer ( your address ) if you are a buyer 🛒 or /seller ( your address ) if you are a seller 🏪 to verify address and continue the deal.\n"
    "( Provide your crypto address which will be used in case of release or refund. ) 💳🔐\n\n"
    "<b>Step 4</b> : Choose the token and network by /token command and then either party has to accept it. ✅💱\n\n"
    "<b>Step 5</b> : Use /deposit command to deposit the asset within the bot.\n"
    "( Note : Bot will give the deposit address and it has a time limit to deposit ⏳, you have to deposit within that given time. ) ⏰💸\n\n"
    "<b>Step 6</b> : Once verified by the bot, you can continue the deal.\n"
    "( Bot will send the real-time deposit details in the chat. ) 📊💬\n\n"
    "<b>Step 7</b> : After a successful deal, you can release the asset to the party by using /release ( amount / all ).\n"
    "( Thus, the bot will itself release the asset to the party and send the verification in the chat. ) 🎉💼\n\n"
    "🚨 IN CASE OF ANY DISPUTE OR ISSUE, YOU CAN FEEL FREE TO USE /dispute COMMAND, AND SUPPORT WILL JOIN YOU SHORTLY. 🛎️👩‍💻"
)

DD_TEXT = (
    "Hello there,\n"
    "Kindly tell deal details i.e.\n\n"
    "Dealinfo -\n"
    "Amount -\n"
    "Conditions ( If Any ) -\n\n"
    "Once filled Seller will use /seller [CRYPTO ADDRESS] and /buyer [CRYPTO ADDRESS] to specify your roles, and start the deal."
)

DD_P2P_TEXT = (
    "Hello there,\n"
    "Kindly tell deal details i.e.\n\n"
    "<code>Quantity -</code>\n"
    "<code>Rate -</code>\n"
    "<code>Conditions (if any) -</code>\n\n"
    "Remember without it disputes wouldn't be resolved. Once filled proceed with "
    "Specifications of the seller or buyer with <code>/seller</code> or "
    "<code>/buyer [CRYPTO ADDRESS]</code>"
)

HOW_TO_USE_URL = "https://t.me/how_to_use_pagalescrowbot"


def how_to_use_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("How to use bot ❔", url=HOW_TO_USE_URL)]])

GROUP_PIN_TEXT = (
    "📍 Hey there traders! Welcome to our escrow service.\n"
    "✅ Please start with /dd command and fill the DealInfo Form"
)

DM_ONLY_IN_GROUP_TEXT = "<b>Please use this command in a group.</b>"

TERMS_TEXT = (
    "📜 <b>TERMS</b>\n\n"
    "Our terms of usage are simple.\n\n"
    "🎟 <b>Fees</b>\n"
    "1.0% for P2P and 1.0% for OTC Flat.\n\n"
    "Transactions fee will be applicable.\n\n"
    "<b>TAKE THIS INTO ACCOUNT WHEN DEPOSITING FUNDS</b>\n\n"
    "1️⃣ Record/screenshot the desktop while your perform any testing of logins or data, or recording of physcial items being opened, this is to provide evidence that the data does not work, if the data is working and you are happy to release the funds, you can delete the recording.\n\n"
    "<b>FAILURE TO PRODUCE SUFFICIENT EVIDENCE OF TESTING WILL RESULT IN LOSS OF FUNDS</b>\n\n"
    "2️⃣ Before you purchase any information, please take the time to learn what you are buying\n\n"
    "<b>IT IS NOT THE RESPONSIBILITY OF THE SELLER TO EXPLAIN HOW TO USE THE INFORMATION, ALTHOUGH IT MAY HELP MAKE TRANSACTIONS RUN SMOOTHER IF VENDORS HELP BUYERS</b>\n\n"
    "3️⃣ Buyer should ONLY EVER release funds when they RECEIVE WHAT YOU PAID FOR.\n\n"
    "<b>WE ARE NOT RESPONSIBLE FOR YOU RELEASING EARLY AND CAN NOT RETRIEVE FUNDS BACK</b>\n\n"
    "4️⃣ Users should use trusted local wallets such as electrum.org or exodus wallet to prevent any issues with KYC wallets like Coinbase or Paxful.\n\n"
    "<b>ONLINE WALLETS CAN BE SLOW AND BLOCK ACCOUNTS</b>\n\n"
    "5️⃣ Our fee's are taken from the balance in the wallet (1.0% for P2P and 1.0% for OTC), so make sure you take that into account when depositing funds.\n\n"
    "<b>WE ARE A SERVICE BARE THAT IN MIND</b>\n\n"
    "6️⃣ Make sure Coin and Netwwork are same for Buyer and Seller, else you may lose your funds."
)

DD_NOT_DONE_TEXT = "<b>Sorry! please first use /dd first!</b>"

FEE_NOTICE_TEXT = (
    "<b>Your Fee is 1.0% as both buyer and seller are not using @{bot} in your bio.</b>"
)

CONTACT_TEXT = (
    "☎️ <b>CONTACT ARBITRATOR</b>\n\n"
    "💬 Type /dispute\n\n"
    "💡 Incase you're not getting a response can reach out to @bsr_official"
)

WHAT_IS_ESCROW_TEXT = (
    "❓ Escrow is a trusted third-party service that holds funds/coins between "
    "a buyer and seller until both parties deliver their end of the deal."
)

CHAINS_SUPPORTED = "bsc, ltc, btc, tron"


def role_format_text(role: str) -> str:
    """e.g. '/buyer [Your Crypto Address]\n\n⛓️ Chains Supported: bsc, ltc, btc, tron'"""
    return f"/{role} [Your Crypto Address]\n\n⛓️ Chains Supported: {CHAINS_SUPPORTED}"


# ---------------- /start ----------------
def build_welcome_text() -> str:
    return (
        f"💫 <b>@{BOT_USERNAME}</b> 💫\n"
        f"<b>Your Trustworthy Telegram Escrow Service</b>\n\n"
        f"Welcome to @{BOT_USERNAME}.\n"
        f"This bot provides a reliable escrow service for your transactions on Telegram.\n"
        f"Avoid scams, your funds are safeguarded throughout your deals. If you run into any "
        f"issues, simply type /dispute and an arbitrator will join the group chat within 24 hours.\n\n"
        f"🎟 <b>ESCROW FEE:</b>\n"
        f"1.0% for P2P and 1.0% for OTC Flat\n\n"
        f'🌐 <a href="{UPDATES_LINK}">UPDATES</a> - <a href="{VOUCHES_LINK}">VOUCHES</a> ☑️\n\n'
        f"💬 Proceed with /escrow (to start with a new escrow)\n\n"
        f"⚠️ <b>IMPORTANT</b> - Make sure coin is same of Buyer and Seller else you may loose your coin.\n\n"
        f"💡 Type /menu to summon a menu with all bots features"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_new_user = user_id not in known_users
    known_users.add(user_id)

    # referral credit: /start ref_XXXX se aaya ho toh referrer ko ticket do
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            code = arg[4:]
            referrer_id = referral_codes.get(code)
            if referrer_id and referrer_id != user_id and user_id not in referred_users:
                referred_users[user_id] = referrer_id
                ref_data = get_or_create_referral(referrer_id)
                ref_data["total_invites"] += 1
                ref_data["tickets"] += 2 if is_new_user else 1

    await update.message.reply_text(
        build_welcome_text(), parse_mode=ParseMode.HTML, reply_markup=menu_keyboard()
    )

    # escrow type prompt: apna alag message taaki bold text ke turant niche
    # sirf P2P / Product Deal ke 2 hi buttons aayein
    await update.message.reply_text(
        "<b>Please select your escrow type from below.</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([escrow_row()]),
    )


# ---------------- /menu ----------------
def menu_rows() -> list:
    return [
        [InlineKeyboardButton("COMMANDS LIST 🤖", callback_data="menu_commands")],
        [InlineKeyboardButton("☎️ CONTACT", callback_data="menu_contact")],
        [
            InlineKeyboardButton("Updates 🔄", url=UPDATES_LINK),
            InlineKeyboardButton("Vouches ✅", url=VOUCHES_LINK),
        ],
        [
            InlineKeyboardButton("WHAT IS ESCROW ❓", callback_data="menu_what"),
            InlineKeyboardButton("Instructions 🧑‍💻", callback_data="menu_instructions"),
        ],
        [InlineKeyboardButton("Terms 📝", callback_data="menu_terms")],
        [InlineKeyboardButton("Invites 👤", callback_data="menu_invites")],
    ]


def menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(menu_rows())


def escrow_row() -> list:
    return [
        InlineKeyboardButton("P2P", callback_data="escrow_p2p"),
        InlineKeyboardButton("Product Deal", callback_data="escrow_product"),
    ]


def start_keyboard() -> InlineKeyboardMarkup:
    """/start ke liye: menu ke saare buttons + niche escrow (P2P / Product Deal) buttons."""
    return InlineKeyboardMarkup(menu_rows() + [escrow_row()])


async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = "📋 <b>MENU</b>"
    await update.effective_chat.send_message(
        caption, parse_mode=ParseMode.HTML, reply_markup=menu_keyboard()
    )


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_menu(update, context)


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu_back")]])


async def menu_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "menu_back":
        # purana message hi edit kar do (naya message nahi bhejna), wahi
        # start wala poora welcome text + niche normal menu buttons
        await query.message.edit_text(
            build_welcome_text(), parse_mode=ParseMode.HTML, reply_markup=menu_keyboard()
        )
        return

    # in sab ke liye same message edit hoga, saath me Back button
    content_map = {
        "menu_commands": COMMANDS_TEXT,
        "menu_instructions": INSTRUCTIONS_TEXT.format(bot=BOT_USERNAME),
        "menu_terms": TERMS_TEXT,
        "menu_contact": CONTACT_TEXT,
        "menu_what": WHAT_IS_ESCROW_TEXT,
    }

    if query.data == "menu_invites":
        text = build_invite_text(query.from_user.id)
    elif query.data in content_map:
        text = content_map[query.data]
    else:
        text = "Coming soon."

    await query.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=back_keyboard(),
    )


# ---------------- /escrow ----------------
async def escrow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Please select your escrow type from below.",
        reply_markup=InlineKeyboardMarkup([escrow_row()]),
    )


async def escrow_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    escrow_type = "P2P" if query.data == "escrow_p2p" else "Product Deal"

    # "Please select your escrow type" wala message (jispe ye 2 buttons the) hata do
    try:
        await query.message.delete()
    except Exception as e:
        logger.error(f"Could not delete escrow-type selection message: {e}")

    # bold "please wait" message
    wait_msg = await context.bot.send_message(
        query.message.chat.id,
        "<b>Creating a safe trading place for you, please wait...</b>",
        parse_mode=ParseMode.HTML,
    )

    # group ka display naam: Product Deal ko "OTC" bola jata hai
    title_type = "OTC" if escrow_type == "Product Deal" else escrow_type

    try:
        chat_id = await create_escrow_group(BOT_USERNAME, f"{title_type} Escrow By PAGAL Bot")
    except UserbotLeaveFailed as e:
        # userbot group me hi reh gayi - user ko is group ka link nahi
        # dena (privacy break), seedha fail bata do. Background job isko
        # baad me khud retry/delete kar dega.
        failed_groups.add(e.chat_id)
        logger.error(f"Userbot leave failed for chat {e.chat_id}: {e.original}")
        await wait_msg.edit_text(
            "<b>Unable to process your request at the moment, please try after some time.</b>",
            parse_mode=ParseMode.HTML,
        )
        return
    except Exception as e:
        logger.error(f"Group creation failed: {e}")
        await wait_msg.edit_text(
            "❌ Failed to create the group, please try again or contact admin."
        )
        return

    # max 2 members allowed in this chat (whoever joins first)
    escrow_groups[chat_id] = {
        "members": set(),
        "max_members": 2,
        "escrow_type": escrow_type,   # "P2P" or "Product Deal"
        "dd_done": False,
        "trade_id": None,   # random 9-digit number, set once on first /dd
        "buyer": None,       # {"user_id":.., "username":.., "address":..}
        "seller": None,      # {"user_id":.., "username":.., "address":..}
        "crypto": None,      # "LTC" / "BTC" / "USDT"
        "network": None,     # "BSC" / "TRON" (sirf USDT ke liye)
        "approved": False,   # seller ne deal accept ki ya nahi
        "deposit_address": None,
        "balance": 0.0,      # cumulative deposited USDT balance
        "created_at": datetime.now(timezone.utc),  # group creation time
    }

    # welcome message bhejo + pin karo, fir usse pehle ke saare (service)
    # messages delete kar do - taaki group clean lage aur sirf ye pinned
    # message hi dikhe jab koi naya member join kare
    try:
        pin_msg = await context.bot.send_message(chat_id, GROUP_PIN_TEXT)
        await context.bot.pin_chat_message(chat_id, pin_msg.message_id, disable_notification=True)
        for msg_id in range(1, pin_msg.message_id):
            try:
                await context.bot.delete_message(chat_id, msg_id)
            except Exception:
                pass  # already gone / never existed, ignore
    except Exception as e:
        logger.error(f"Pin message failed: {e}")
        try:
            await context.bot.send_message(chat_id, f"⚠️ Could not pin welcome message: {e}")
        except Exception as e2:
            logger.error(f"Even the pin-error message failed: {e2}")

    invite_link = await context.bot.create_chat_invite_link(chat_id, member_limit=2)

    # remove the wait message, replace it with the result
    await wait_msg.delete()

    creator_name = query.from_user.full_name
    creator_username = f" @{query.from_user.username}" if query.from_user.username else ""

    await context.bot.send_message(
        query.message.chat.id,
        f"<u>Escrow Group Created</u>\n\n"
        f"Creator: {creator_name}{creator_username}\n\n"
        f"Join this escrow group and share the link with the buyer and seller.\n\n"
        f"{invite_link.invite_link}\n\n"
        f"<blockquote>⚠️ Note: This link is for 2 members only—third parties are not allowed to join.</blockquote>",
        parse_mode=ParseMode.HTML,
    )


# ---------------- /invite ----------------
async def invite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        build_invite_text(update.effective_user.id),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=back_keyboard(),
    )


# ---------------- /commands ----------------
async def commands_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        COMMANDS_TEXT,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=back_keyboard(),
    )


# ---------------- /instructions ----------------
async def instructions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        INSTRUCTIONS_TEXT.format(bot=BOT_USERNAME),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=back_keyboard(),
    )


# ---------------- /whatisescrow ----------------
async def whatisescrow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        WHAT_IS_ESCROW_TEXT, parse_mode=ParseMode.HTML, reply_markup=back_keyboard()
    )


# ---------------- /contact ----------------
async def contact_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        CONTACT_TEXT, parse_mode=ParseMode.HTML, reply_markup=back_keyboard()
    )


# ---------------- /dispute (abhi sirf DM guard, group logic baad me) ----------------
async def dispute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text(DM_ONLY_IN_GROUP_TEXT, parse_mode=ParseMode.HTML)
        return
    # group ke andar dispute ka actual logic abhi implement nahi karna (baad me aayega)
    return


# ---------------- /dd (sirf group me chalega) ----------------
async def dd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text(DM_ONLY_IN_GROUP_TEXT, parse_mode=ParseMode.HTML)
        return

    chat_id = update.effective_chat.id
    if chat_id not in escrow_groups:
        return

    group = escrow_groups[chat_id]
    first_time = not group["dd_done"]
    group["dd_done"] = True

    if first_time:
        # ek random 9-digit trade id banao aur group ke naam me jod do (sirf ek baar)
        trade_id = "".join(random.choices(string.digits, k=9))
        group["trade_id"] = trade_id
        try:
            chat = await context.bot.get_chat(chat_id)
            await context.bot.set_chat_title(chat_id, f"{chat.title} {trade_id}")
        except Exception as e:
            logger.error(f"Could not rename group with trade id: {e}")

    text = DD_P2P_TEXT if group["escrow_type"] == "P2P" else DD_TEXT
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=how_to_use_keyboard()
    )


# ---------------- /terms ----------------
async def terms_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        TERMS_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard(),
        disable_web_page_preview=True,
    )


# ---------------- /buyer & /seller ----------------
def user_mention(user_id: int, username: str | None) -> str:
    return f"@{username}" if username else f"User {user_id}"


async def role_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str):
    chat_id = update.effective_chat.id

    if chat_id not in escrow_groups:
        # ye command sirf actual escrow group ke andar matlab rakhta hai
        return

    group = escrow_groups[chat_id]

    if not group["dd_done"]:
        await update.message.reply_text(DD_NOT_DONE_TEXT, parse_mode=ParseMode.HTML)
        return

    if not context.args:
        await update.message.reply_text(role_format_text(role))
        return

    address = context.args[0]
    user = update.effective_user

    group[role] = {"user_id": user.id, "username": user.username, "address": address}

    role_upper = role.upper()
    mention = user_mention(user.id, user.username)

    declaration_text = (
        f"📍<b>ESCROW-ROLE DECLARATION</b>\n\n"
        f"⚡️ {role_upper} {mention} | Userid: [{user.id}]\n\n"
        f"✅ <b>{role_upper} WALLET</b>\n"
        f"<code>{address}</code>\n\n"
        f"Note: If you don't see any address, then your address will used from saved "
        f"addresses after selecting token and chain for the current escrow."
    )
    decl_msg = await update.message.reply_text(declaration_text, parse_mode=ParseMode.HTML)

    if role == "seller":
        await decl_msg.reply_text("Please set buyer using /buyer [DEPOSIT ADDRESS]")

    # dono roles set ho chuke ho to token step ka prompt de do
    if group.get("buyer") and group.get("seller"):
        await update.effective_chat.send_message(
            "<b>Use /token to Choose crypto.</b>", parse_mode=ParseMode.HTML
        )


async def buyer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await role_cmd(update, context, "buyer")


async def seller_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await role_cmd(update, context, "seller")


# ---------------- /token + crypto/network selection ----------------
def token_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("LTC", callback_data="token_LTC"),
                InlineKeyboardButton("BTC", callback_data="token_BTC"),
            ],
            [InlineKeyboardButton("USDT", callback_data="token_USDT")],
        ]
    )


async def token_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    group = escrow_groups.get(chat_id)
    if not group or not (group.get("buyer") and group.get("seller")):
        return
    await update.message.reply_text("choose token from list below", reply_markup=token_keyboard())


async def show_escrow_declaration(message, group: dict):
    """Crypto/network confirm ho jaane ke baad wali ESCROW DECLARATION,
    Accept/Reject buttons ke saath - same message edit hota hai."""
    seller = group["seller"]
    mention = user_mention(seller["user_id"], seller.get("username"))

    text = f"📍<b>ESCROW DECLARATION</b>\n\n" f"⚡️ Seller {mention} | Userid: [{seller['user_id']}]\n\n"
    text += f"✅ {group['crypto']} CRYPTO"
    if group.get("network"):
        text += f"\n✅ {group['network']} NETWORK"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("ACCEPT✅", callback_data="deal_accept"),
                InlineKeyboardButton("REJECT❌", callback_data="deal_reject"),
            ]
        ]
    )
    await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def token_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    group = escrow_groups.get(chat_id)
    if not group:
        return

    if query.data == "token_back":
        await query.message.edit_text("choose token from list below", reply_markup=token_keyboard())
        return

    crypto = query.data.split("_", 1)[1]  # "LTC" / "BTC" / "USDT"
    group["crypto"] = crypto
    group["network"] = None

    if crypto == "USDT":
        text = (
            f"📍<b>ESCROW-CRYPTO DECLARATION</b>\n\n"
            f"✅ <b>CRYPTO</b>\n{crypto}\n\n"
            f"choose network from the list below for {crypto}"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("BSC[BEP20]", callback_data="network_BSC"),
                    InlineKeyboardButton("TRON[TRC20]", callback_data="network_TRON"),
                ],
                [InlineKeyboardButton("⬅️ BACK", callback_data="token_back")],
            ]
        )
        await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    else:
        await show_escrow_declaration(query.message, group)


async def network_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    group = escrow_groups.get(chat_id)
    if not group:
        return
    group["network"] = query.data.split("_", 1)[1]  # "BSC" / "TRON"
    await show_escrow_declaration(query.message, group)


async def deal_decision_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat.id
    group = escrow_groups.get(chat_id)

    if not group:
        await query.answer("This deal is no longer valid.", show_alert=True)
        return

    seller = group.get("seller")
    if not seller or query.from_user.id != seller["user_id"]:
        await query.answer("Sorry! Only the seller can accept or reject this deal.", show_alert=True)
        return

    await query.answer()

    if query.data == "deal_reject":
        await query.message.edit_text("❌ Deal rejected by seller.")
        return

    # accept - purana declaration message hata do
    await query.message.delete()
    group["approved"] = True

    buyer = group["buyer"]
    seller_mention = user_mention(seller["user_id"], seller.get("username"))
    buyer_mention = user_mention(buyer["user_id"], buyer.get("username"))

    crypto = group.get("crypto", "")
    network_tag = f" [{group['network']}]" if group.get("network") else ""

    ist_time = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    time_str = ist_time.strftime("%d/%m/%y %H:%M:%S")

    text = (
        f"📍 <b>TRANSACTION INFORMATION [{group.get('trade_id') or 'N/A'}]</b>\n\n"
        f"⚡️ <b>SELLER</b>\n"
        f"{seller_mention} | [{seller['user_id']}]\n"
        f"<code>{seller['address']}</code>[{crypto}]{network_tag}\n\n"
        f"⚡️ <b>BUYER</b>\n"
        f"{buyer_mention} | [{buyer['user_id']}]\n"
        f"<code>{buyer['address']}</code>[{crypto}]{network_tag}\n\n"
        f"⏰ <b>Trade Start Time:</b> {time_str}\n"
        f"(Real time IST)\n\n"
        f"⚠️ <b>IMPORTANT:</b> Make sure to finalise and agree each-others terms before depositing.\n\n"
        f"🗒 Please use /deposit command to generate a deposit address for your trade."
    )
    await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)


# ---------------- /deposit ----------------
# Sirf ek hi BSC deposit address
DEPOSIT_ADDRESS_POOL = [
    "0x686de9945100a62fdb185dfa082f92f0a0cda497",
]

DEPOSIT_WINDOW_MINUTES = 20


async def deposit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    group = escrow_groups.get(chat_id)

    if not group or not group.get("approved"):
        return  # deal abhi accept nahi hui, deposit ka matlab nahi

    wait_msg = await update.message.reply_text(
        "Requesting a deposit address for you, please wait..."
    )
    await asyncio.sleep(1.5)
    try:
        await wait_msg.delete()
    except Exception as e:
        logger.error(f"Could not delete deposit wait message: {e}")

    seller = group["seller"]
    buyer = group["buyer"]
    seller_mention = user_mention(seller["user_id"], seller.get("username"))
    buyer_mention = user_mention(buyer["user_id"], buyer.get("username"))

    crypto = group.get("crypto", "")
    network_tag = f" [{group['network']}]" if group.get("network") else ""

    deposit_address = random.choice(DEPOSIT_ADDRESS_POOL)
    group["deposit_address"] = deposit_address

    ist_time = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    time_str = ist_time.strftime("%d/%m/%y %H:%M:%S")

    text = (
        f"📍 <b>TRANSACTION INFORMATION [{group.get('trade_id') or 'N/A'}]</b>\n\n"
        f"⚡️ <b>SELLER</b>\n{seller_mention} | [{seller['user_id']}]\n"
        f"⚡️ <b>BUYER</b>\n{buyer_mention} | [{buyer['user_id']}]\n\n"
        f"🟢 <b>ESCROW ADDRESS</b>\n"
        f"<code>{deposit_address}</code>[{crypto}]{network_tag}\n\n"
        f"Seller [{seller_mention}] Will Pay on the Escrow Address, And Click On Check Payment.\n\n"
        f"Amount Recieved: 0.00000 [0.00$]\n\n"
        f"⏰ Trade Start Time: {time_str}\n"
        f"⏰ Address Reset In: {DEPOSIT_WINDOW_MINUTES:.2f} Min\n\n"
        f"📄 Note: Address will reset after the given time, so make sure to deposit in the bot "
        f"before the address exprires.\n"
        f"Useful commands:\n"
        f"🗒 /release = Will Release The Funds To Buyer.\n"
        f"🗒 /refund = Will Refund The Funds To Seller.\n\n"
        f"Remember, once commands are used payment will be released, there is no revert!"
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Check Payment", callback_data="check_payment")]]
    )
    deposit_msg = await context.bot.send_message(
        chat_id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )
    try:
        await context.bot.pin_chat_message(chat_id, deposit_msg.message_id, disable_notification=True)
    except Exception as e:
        logger.error(f"Could not pin deposit message: {e}")

    # branded "P.A.G.A.L Escrow Bot" image with buyer/seller usernames -
    # group ki DP (chat photo) set kar do, chat me alag se photo nahi bhejni
    try:
        photo_buf = generate_deal_image(buyer_mention, seller_mention)
        await context.bot.set_chat_photo(chat_id, photo=photo_buf)
    except Exception as e:
        logger.error(f"Deal image generation/set-chat-photo failed: {e}")

    await context.bot.send_message(
        chat_id,
        FEE_NOTICE_TEXT.format(bot=BOT_USERNAME),
        parse_mode=ParseMode.HTML,
    )


async def check_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # abhi real blockchain monitoring nahi hai - isliye hamesha "not received" bolega
    await query.answer("No deposit detected on this address yet.", show_alert=True)


# ---------------- Auto-kick extra members ----------------
async def chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Escrow group me max 2 members (jo pehle join kare) + bot allowed. Baaki kick."""
    chat_id = update.effective_chat.id
    if chat_id not in escrow_groups:
        return

    group = escrow_groups[chat_id]
    new_member = update.chat_member.new_chat_member
    user = new_member.user

    if user.id == context.bot.id:
        return  # apna hi bot hai, ignore

    if new_member.status == "member":
        if user.id in group["members"]:
            return  # already counted (re-join same person)

        if len(group["members"]) < group["max_members"]:
            group["members"].add(user.id)
        else:
            try:
                await context.bot.ban_chat_member(chat_id, user.id)
                await context.bot.unban_chat_member(chat_id, user.id)
                await context.bot.send_message(
                    chat_id,
                    f"⛔ {user.mention_html()} is not part of this escrow, removed.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.error(f"Kick failed: {e}")


# ================================================================
# /depodepo — DM command: active escrow groups list → deposit confirm
# ================================================================

# ConversationHandler states
DEPODEPO_SELECT, DEPODEPO_AMOUNT = range(2)


async def depodepo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Only in DM. Shows active escrow groups (created in last 1 hr with a trade_id)."""
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "⚠️ Ye command sirf bot ke DM mein chalaayein.", parse_mode=ParseMode.HTML
        )
        return ConversationHandler.END

    now = datetime.now(timezone.utc)
    one_hour_ago = now - timedelta(hours=1)

    # Groups jo last 1 ghante mein bane aur jinmein /dd ho chuka ho (trade_id set hai)
    active = [
        (chat_id, data)
        for chat_id, data in escrow_groups.items()
        if data.get("trade_id")
        and data.get("created_at", datetime.min.replace(tzinfo=timezone.utc)) >= one_hour_ago
    ]

    if not active:
        await update.message.reply_text(
            "📭 Koi active escrow group nahi mila pichle 1 ghante mein.\n\n"
            "Sirf wahi groups dikhte hain jinmein /dd ho chuka ho aur jo last 1 hr mein bane hon."
        )
        return ConversationHandler.END

    keyboard = []
    for chat_id, data in active:
        trade_id = data["trade_id"]
        escrow_type = data.get("escrow_type", "Escrow")
        balance = data.get("balance", 0.0)
        keyboard.append([
            InlineKeyboardButton(
                f"🔖 Trade #{trade_id} | {escrow_type} | 💰{balance:.2f} USDT",
                callback_data=f"depodepo_{chat_id}"
            )
        ])

    await update.message.reply_text(
        "📋 <b>Active Escrow Groups (Last 1 Hour)</b>\n\n"
        "Niche se group select karein jisme deposit confirm karni hai:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return DEPODEPO_SELECT


async def depodepo_select_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User ne group select kiya — ab USDT amount maango."""
    query = update.callback_query
    await query.answer()

    # callback_data = "depodepo_{chat_id}" — chat_id negative ho sakta hai
    raw = query.data[len("depodepo_"):]  # "depodepo_" ke baad sab kuch
    try:
        selected_chat_id = int(raw)
    except ValueError:
        await query.message.edit_text("❌ Invalid selection. /depodepo se dobara try karein.")
        return ConversationHandler.END

    if selected_chat_id not in escrow_groups:
        await query.message.edit_text("❌ Ye group ab available nahi hai. /depodepo se dobara try karein.")
        return ConversationHandler.END

    context.user_data["depodepo_chat_id"] = selected_chat_id
    trade_id = escrow_groups[selected_chat_id].get("trade_id", "N/A")

    await query.message.edit_text(
        f"✅ Group selected: <b>Trade #{trade_id}</b>\n\n"
        f"💵 Kitna USDT deposit confirm karna hai?\n"
        f"Amount likhein (jaise: 5 ya 10.3):",
        parse_mode=ParseMode.HTML,
    )
    return DEPODEPO_AMOUNT


async def depodepo_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User ne amount diya — GC mein deposit confirmation bhejo."""
    text = update.message.text.strip()
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError("Amount must be positive")
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid amount. Sirf number enter karein (jaise: 5 ya 10.3):"
        )
        return DEPODEPO_AMOUNT

    chat_id = context.user_data.get("depodepo_chat_id")
    if not chat_id or chat_id not in escrow_groups:
        await update.message.reply_text(
            "❌ Group nahi mila. /depodepo se dobara try karein."
        )
        return ConversationHandler.END

    group = escrow_groups[chat_id]

    # Balance update karo
    group["balance"] = group.get("balance", 0.0) + amount
    new_balance = group["balance"]

    # GC mein confirmation message
    confirm_msg = (
        f"Deposit 💵 has been confirmed \n\n"
        f"💰Token: USDT-BSC\n"
        f"🪙Amounts: {amount:.4f}[{amount:.2f}$]\n"
        f"💸 Balance: {new_balance:.4f}[{new_balance:.2f}$]\n\n"
        f"Now you can proceeds with the deal\n"
        f"✅\n\n\n"
        f"Useful command:\n"
        f"📂 /release= Will Releases The Fund To Buyer\n"
        f"📂/refund= Will Refunds The Fund To Seller"
    )

    try:
        await context.bot.send_message(chat_id, confirm_msg)
        trade_id = group.get("trade_id", "N/A")
        await update.message.reply_text(
            f"✅ Deposit confirmation bhej di gayi!\n\n"
            f"🔖 Trade #{trade_id}\n"
            f"💰 Amount: {amount:.4f} USDT\n"
            f"💸 New Balance: {new_balance:.4f} USDT"
        )
    except Exception as e:
        logger.error(f"depodepo: Could not send confirmation to group {chat_id}: {e}")
        await update.message.reply_text(
            f"❌ Group mein message nahi bheja ja saka: {e}\n"
            "Bot ko group admin banana padega."
        )

    return ConversationHandler.END


async def depodepo_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ /depodepo cancelled.")
    return ConversationHandler.END


depodepo_conv = ConversationHandler(
    entry_points=[CommandHandler("depodepo", depodepo_cmd, filters=filters.ChatType.PRIVATE)],
    states={
        DEPODEPO_SELECT: [CallbackQueryHandler(depodepo_select_handler, pattern=r"^depodepo_-?\d+$")],
        DEPODEPO_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, depodepo_amount_handler)],
    },
    fallbacks=[CommandHandler("cancel", depodepo_cancel)],
    per_chat=True,
)


# ---------------- /addbot (owner-only userbot login) ----------------
# States
ADDBOT_PHONE, ADDBOT_OTP, ADDBOT_PASSWORD = range(3)


async def addbot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        # nobody except the owner should even know this command exists
        return ConversationHandler.END

    if userbot_session_exists():
        await update.message.reply_text(
            "✅ Userbot is already logged in. To log in again, first delete "
            "the session file (Railway volume)."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "📱 Send the userbot account's phone number (with country code, e.g. +919876543210):"
    )
    return ADDBOT_PHONE


async def addbot_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    context.user_data["addbot_phone"] = phone

    status = await update.message.reply_text("⏳ Sending OTP...")
    try:
        phone_code_hash = await login_send_code(phone)
    except Exception as e:
        logger.error(f"send_code failed: {e}")
        await status.edit_text(f"❌ Error sending OTP: {e}")
        return ConversationHandler.END

    context.user_data["addbot_phone_code_hash"] = phone_code_hash
    await status.edit_text(
        "✅ OTP has been sent to your Telegram account.\n"
        "Send that OTP here (exactly as received, spaces between digits are "
        "fine, it'll be auto-cleaned):"
    )
    return ADDBOT_OTP


async def addbot_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = re.sub(r"\D", "", update.message.text)  # keep digits only
    phone = context.user_data.get("addbot_phone")
    phone_code_hash = context.user_data.get("addbot_phone_code_hash")

    try:
        await login_sign_in(phone, phone_code_hash, code)
    except SessionPasswordNeeded:
        await update.message.reply_text(
            "🔒 This account has 2-step verification (cloud password) enabled.\n"
            "Send that password:"
        )
        return ADDBOT_PASSWORD
    except Exception as e:
        logger.error(f"sign_in failed: {e}")
        await update.message.reply_text(f"❌ OTP is incorrect or expired: {e}\nTry again with /addbot.")
        return ConversationHandler.END

    await login_finish()
    try:
        await start_userbot()
        await update.message.reply_text(
            "✅ Userbot logged in and connected successfully! You can test it now using /escrow."
        )
    except Exception as e:
        logger.error(f"start_userbot after login failed: {e}")
        await update.message.reply_text(
            "✅ Login successful and session saved, but auto-connect failed:\n"
            f"{e}\nPlease restart the bot on Railway once."
        )
    return ConversationHandler.END


async def addbot_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    try:
        await login_check_password(password)
    except Exception as e:
        logger.error(f"check_password failed: {e}")
        await update.message.reply_text(f"❌ Incorrect password: {e}\nTry again with /addbot.")
        return ConversationHandler.END

    await login_finish()
    try:
        await start_userbot()
        await update.message.reply_text(
            "✅ Userbot logged in (2FA verified) and connected successfully! You can test it now using /escrow."
        )
    except Exception as e:
        logger.error(f"start_userbot after 2FA login failed: {e}")
        await update.message.reply_text(
            "✅ Login successful and session saved, but auto-connect failed:\n"
            f"{e}\nPlease restart the bot on Railway once."
        )
    return ConversationHandler.END


async def addbot_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


addbot_conv = ConversationHandler(
    entry_points=[CommandHandler("addbot", addbot_cmd)],
    states={
        ADDBOT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, addbot_phone)],
        ADDBOT_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, addbot_otp)],
        ADDBOT_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, addbot_password)],
    },
    fallbacks=[CommandHandler("cancel", addbot_cancel)],
)


# ---------------- Userbot lifecycle ----------------
async def on_startup(app):
    """Bot ke saath saath userbot session bhi ek hi baar connect ho jaye
    (agar pehle se login hai - warna owner ko /addbot pehle chalana hoga)."""
    if userbot_session_exists():
        await start_userbot()
        logger.info("Userbot session connected.")
    else:
        logger.warning(
            "Userbot abhi login nahi hai. Owner /addbot command se login kare, "
            "phir bot restart kar."
        )


async def on_shutdown(app):
    """Bot band hote waqt userbot session bhi gracefully disconnect ho jaye
    (sirf tab jab wo actually chal rahi ho)."""
    if userbot_is_running():
        await stop_userbot()
        logger.info("Userbot session disconnected.")


# ---------------- Background cleanup for failed-leave groups ----------------
async def cleanup_failed_groups_job(context: ContextTypes.DEFAULT_TYPE):
    if not failed_groups:
        return
    for chat_id in list(failed_groups):
        result = await cleanup_group(chat_id)
        logger.info(f"Cleanup for chat {chat_id}: {result}")
        if result in ("left", "deleted"):
            failed_groups.discard(chat_id)
            escrow_groups.pop(chat_id, None)
        # agar "error: ..." aaya to agli baar job run hone pe phir try hoga


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("escrow", escrow_cmd))
    app.add_handler(CommandHandler("commands", commands_cmd))
    app.add_handler(CommandHandler("instructions", instructions_cmd))
    app.add_handler(CommandHandler("whatisescrow", whatisescrow_cmd))
    app.add_handler(CommandHandler("contact", contact_cmd))
    app.add_handler(CommandHandler("dispute", dispute_cmd))
    app.add_handler(CommandHandler("dd", dd_cmd))
    app.add_handler(CommandHandler("terms", terms_cmd))
    app.add_handler(CommandHandler("buyer", buyer_cmd))
    app.add_handler(CommandHandler("seller", seller_cmd))
    app.add_handler(CommandHandler("token", token_cmd))
    app.add_handler(CommandHandler("deposit", deposit_cmd))
    app.add_handler(CommandHandler("invite", invite_cmd))
    app.add_handler(depodepo_conv)
    app.add_handler(addbot_conv)
    app.add_handler(CallbackQueryHandler(escrow_type_handler, pattern="^escrow_"))
    app.add_handler(CallbackQueryHandler(menu_button_handler, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(token_button_handler, pattern="^token_"))
    app.add_handler(CallbackQueryHandler(network_button_handler, pattern="^network_"))
    app.add_handler(CallbackQueryHandler(deal_decision_handler, pattern="^deal_"))
    app.add_handler(CallbackQueryHandler(check_payment_handler, pattern="^check_payment$"))
    app.add_handler(ChatMemberHandler(chat_member_update, ChatMemberHandler.CHAT_MEMBER))

    # har 5 minute me un groups ko retry/delete karega jaha userbot leave
    # nahi kar payi thi (failed_groups queue)
    app.job_queue.run_repeating(cleanup_failed_groups_job, interval=300, first=60)

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
