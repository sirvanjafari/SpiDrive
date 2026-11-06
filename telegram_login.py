import asyncio
from telethon import TelegramClient
from config import API_ID, API_HASH, PHONE

async def main():
    client = TelegramClient('session', API_ID, API_HASH)
    await client.start(phone=PHONE)
    
    me = await client.get_me()
    print(f"\n✅ وارد شدی به عنوان: {me.first_name} (@{me.username})")
    
    # ساخت کانال خصوصی
    from telethon.tl.functions.channels import CreateChannelRequest
    result = await client(CreateChannelRequest(
        title="MyDrive",
        about="Personal cloud drive storage",
        megagroup=False
    ))
    channel_id = result.chats[0].id
    print(f"\n✅ کانال ساخته شد. ID: -100{channel_id}")
    print(f"\nاین ID رو توی config.py بذار برای CHANNEL")
    
    await client.disconnect()

asyncio.run(main())