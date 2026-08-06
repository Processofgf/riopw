"""
Telegram BOT API se naya group create NAHI ho sakta - ye Telegram ki
limitation hai (koi createGroup method hi nahi hai bots ke liye).

Isliye ek "userbot" chahiye - matlab apne (ya ek dedicated) real Telegram
account ka login session, jo asal me group create kar sake, phir usme
apne escrow BOT ko add karke admin bana de.

Setup:
1. https://my.telegram.org se api_id aur api_hash le (apne number se).
2. pip install pyrogram tgcrypto
3. Pehli baar chalane par phone number + OTP maangega, session file ban jayegi.
4. Uske baad ye headless chal sakta hai (Railway pe bhi).

⚠️ Dhyaan rakh: user account se bulk/automated group creation Telegram ke
ToS ke against ja sakta hai agar overuse hua (spammy lage), rate-limit se
bachne ke liye delays rakhna aur limited scale pe hi use karna.
"""

from pyrogram import Client, enums
from pyrogram.types import ChatPrivileges
from pyrogram.errors import SessionPasswordNeeded
import asyncio
import os

import logging

API_ID = int(os.environ.get("API_ID", "123456"))
API_HASH = os.environ.get("API_HASH", "your_api_hash_here")
SESSION_NAME = "escrow_creator"  # pehli run ke baad escrow_creator.session ban jayegi

logger = logging.getLogger(__name__)

app = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH)


class UserbotLeaveFailed(Exception):
    """Group ban gaya + bot admin ban gaya, bas userbot leave nahi kar payi.
    Isse alag exception rakha hai taaki caller poori escrow creation ko
    fail na maan le - group actually usable hai, sirf userbot abhi group
    me dikh rahi hogi."""

    def __init__(self, chat_id: int, original: Exception):
        super().__init__(str(original))
        self.chat_id = chat_id
        self.original = original


# Ek hi userbot Client hai jo sabke escrow groups banata hai. Agar do log
# (ya do requests) lagbhag ek saath /escrow chalayein, to unke create/add/
# promote/leave steps ek dusre se race kar sakte hain (peer resolution,
# flood-wait, etc.) - isliye pura flow is lock se serialize kar diya, ek
# waqt me sirf ek hi escrow group ban raha hoga, baaki queue me wait karenge.
_userbot_lock = asyncio.Lock()

# escrow bot ko group me ye sab permissions milengi
FULL_ADMIN_PRIVILEGES = ChatPrivileges(
    can_manage_chat=True,
    can_delete_messages=True,
    can_manage_video_chats=True,
    can_restrict_members=True,
    can_promote_members=True,
    can_change_info=True,
    can_invite_users=True,
    can_pin_messages=True,
)


async def start_userbot():
    """Bot startup pe ek hi baar call hoga (main.py se). Userbot session yaha connect hoti hai."""
    await app.start()


async def stop_userbot():
    """Bot shutdown pe call hoga, userbot session gracefully band karne ke liye."""
    await app.stop()


def userbot_session_exists() -> bool:
    """Check karta hai ki userbot pehle se login hai ya nahi (session file bani ya nahi)."""
    return os.path.exists(f"{SESSION_NAME}.session")


def userbot_is_running() -> bool:
    """Check karta hai ki userbot client abhi connected/running hai ya nahi."""
    return app.is_connected


async def is_userbot_in_chat(chat_id: int) -> bool:
    """True agar userbot abhi bhi us chat ka member/admin/creator hai."""
    try:
        member = await app.get_chat_member(chat_id, "me")
        return member.status not in (
            enums.ChatMemberStatus.LEFT,
            enums.ChatMemberStatus.BANNED,
        )
    except Exception:
        # peer resolve nahi hua ya "USER_NOT_PARTICIPANT" - matlab
        # userbot ab us chat ka member nahi hai, safely False maan lo
        return False


async def cleanup_group(chat_id: int) -> str:
    """Un groups ke liye jaha userbot leave nahi kar payi thi - ek aur
    leave try karta hai, wo bhi fail ho to poora group hi delete kar deta
    hai (kyunki userbot group me dikhna escrow ki privacy break karta hai
    aur user ko already bata diya gaya hota hai ki request fail ho gayi)."""
    try:
        if await is_userbot_in_chat(chat_id):
            try:
                await app.leave_chat(chat_id)
            except Exception as e:
                logger.error(f"cleanup_group: leave retry failed for {chat_id}: {e}")

        if await is_userbot_in_chat(chat_id):
            await app.delete_channel(chat_id)
            return "deleted"

        return "left"
    except Exception as e:
        logger.error(f"cleanup_group failed for {chat_id}: {e}")
        return f"error: {e}"


# ---------------- /addbot login flow (owner-only, Telegram se hi OTP daalne ke liye) ----------------
# Ye 3 functions ek hi Client (app) object ko connect/sign_in/disconnect karte hain.
# Isse main.py ka /addbot command phone -> OTP -> (agar 2FA hai) password ka
# conversation chala ke userbot ko bina terminal/SSH ke login karwa sakta hai.

async def login_send_code(phone_number: str) -> str:
    """Owner ke diye number pe Telegram OTP bhejta hai. Returns phone_code_hash (step 2 me chahiye)."""
    if not app.is_connected:
        await app.connect()
    sent = await app.send_code(phone_number)
    return sent.phone_code_hash


async def login_sign_in(phone_number: str, phone_code_hash: str, code: str):
    """OTP verify karta hai. Agar account pe 2-step (cloud password) on hai,
    ye SessionPasswordNeeded raise karega - us case me login_check_password() call karna."""
    await app.sign_in(phone_number, phone_code_hash, code)


async def login_check_password(password: str):
    """2-step verification (cloud password) wale accounts ke liye."""
    await app.check_password(password)


async def login_finish():
    """Login complete hone ke baad connection band kar do (session already save ho chuki hoti hai)."""
    if app.is_connected:
        await app.disconnect()


async def create_escrow_group(bot_username: str, title: str) -> int:
    """
    Naya (super)group banata hai, escrow bot ko add + full admin karta hai,
    naye members ke liye chat history hidden kar deta hai, aur phir userbot
    khud group se leave kar jata hai (sirf bot + jo log link se join karenge
    wahi group me rahenge).

    NOTE: is function ko call karne se pehle start_userbot() already ek baar
    bot startup pe chal chuka hona chahiye - yaha dobara app.start()/stop()
    NAHI kiya jata, warna concurrent escrow requests me client clash karega.

    Returns: chat_id (bot ko ye chahiye invite link banane ke liye)
    """
    async with _userbot_lock:
        # supergroup isliye banaya kyunki normal basic group me
        # "hide history for new members" wala option support nahi karta
        chat = await app.create_supergroup(
            title=title,
            description=f"Escrow deal handled by @{bot_username}",
        )

        # bot ko group me add karo
        await app.add_chat_members(chat.id, bot_username)

        # Telegram ko thoda time do member-join register karne ke liye,
        # warna promote turant call karne se kabhi kabhi race/flood error aata hai
        await asyncio.sleep(1)

        # bot ko full admin bana do
        await app.promote_chat_member(chat.id, bot_username, privileges=FULL_ADMIN_PRIVILEGES)

        # NOTE: "hide history for new members" jaan-bujh kar OFF rakha hai -
        # uski jagah main.py group create hote hi apna pin message bhej ke
        # usse pehle ke saare (service) messages khud delete kar deta hai.
        # Isse naye members ko hamesha wo pinned message dikhega (hide-history
        # ke saath aisa guarantee nahi tha).

        # userbot (creator account) khud nikal jaye - group me sirf bot + future
        # members (seller/buyer link se join karke) hi rahenge
        try:
            await app.leave_chat(chat.id)
        except Exception as e:
            logger.error(f"Userbot leave_chat raised for {chat.id}: {e}")

        # sirf exception na aane se bharosa nahi - actual member status check karo
        if await is_userbot_in_chat(chat.id):
            logger.error(f"Userbot still in chat {chat.id} after leave, retrying once...")
            try:
                await app.leave_chat(chat.id)
            except Exception as e:
                logger.error(f"Userbot leave_chat retry raised for {chat.id}: {e}")

            if await is_userbot_in_chat(chat.id):
                raise UserbotLeaveFailed(
                    chat.id, RuntimeError("userbot still a member after 2 leave attempts")
                )

        return chat.id


# Standalone test:
# if __name__ == "__main__":
#     import asyncio
#     async def _test():
#         await start_userbot()
#         chat_id = await create_escrow_group("pagalescowbot", "Escrow #1")
#         print("Group created:", chat_id)
#         await stop_userbot()
#     asyncio.run(_test())
