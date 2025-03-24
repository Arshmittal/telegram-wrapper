import os
import logging
import asyncio
import time
from typing import Dict, Any
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
from telethon import TelegramClient
from telethon.tl.types import User
from datetime import datetime, timedelta
from telethon.errors import SessionPasswordNeededError, FloodWaitError, ServerError, TimedOutError
from fastapi.middleware.cors import CORSMiddleware

# Enhanced logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("telegram_api")

# Telegram API credentials
API_ID = int(os.environ.get("API_ID", "26725988"))
API_HASH = os.environ.get("API_HASH", "b3692b28078c068aa5a3643ee2870986")

# Store session files
SESSIONS_DIR = "sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

# Client management
clients = {}  # Store active Telegram clients
client_last_used = {}  # Track when clients were last used
CLIENT_TIMEOUT = 300  # Disconnect clients after 5 minutes of inactivity

# Models
class MessageResponse(BaseModel):
    id: int
    text: str
    outgoing: bool
    timestamp: str
    
class LoginRequest(BaseModel):
    phone_number: str

class OTPVerifyRequest(BaseModel):
    phone_number: str
    code: str
    password: str = None  # Optional password for 2FA

class MessageRequest(BaseModel):
    phone_number: str
    contact_id: int
    message: str

# App initialization
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, change to your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# The rest of your code remains unchanged...

# Client management functions
async def get_client(phone_number: str, create_if_missing: bool = True) -> TelegramClient:
    """Get an existing client or create a new one if needed"""
    session_path = os.path.join(SESSIONS_DIR, f"{phone_number}.session")
    
    # Update last used time
    client_last_used[phone_number] = time.time()
    
    # Return existing connected client if available
    if phone_number in clients and clients[phone_number].is_connected():
        return clients[phone_number]
    
    # Handle disconnected clients
    if phone_number in clients:
        try:
            await clients[phone_number].disconnect()
        except Exception as e:
            logger.warning(f"Error disconnecting existing client for {phone_number}: {e}")
        del clients[phone_number]
    
    # Create new client if needed
    if create_if_missing:
        client = TelegramClient(session_path, API_ID, API_HASH)
        
        # Set higher timeouts to prevent connection issues
        client.flood_sleep_threshold = 60
        
        try:
            await client.connect()
            clients[phone_number] = client
            return client
        except Exception as e:
            logger.error(f"Failed to connect client for {phone_number}: {e}")
            raise HTTPException(status_code=500, detail=f"Connection error: {str(e)}")
    
    raise HTTPException(status_code=404, detail="No active session found")

async def cleanup_inactive_clients():
    """Periodically disconnect inactive clients to free resources"""
    while True:
        current_time = time.time()
        phones_to_disconnect = []
        
        for phone, last_used in client_last_used.items():
            if current_time - last_used > CLIENT_TIMEOUT and phone in clients:
                phones_to_disconnect.append(phone)
        
        for phone in phones_to_disconnect:
            try:
                logger.info(f"Disconnecting inactive client for {phone}")
                await clients[phone].disconnect()
                del clients[phone]
            except Exception as e:
                logger.warning(f"Error during cleanup for {phone}: {e}")
        
        await asyncio.sleep(60)  # Check every minute

@app.on_event("startup")
async def startup_event():
    """Start background tasks when app starts"""
    asyncio.create_task(cleanup_inactive_clients())

@app.on_event("shutdown")
async def shutdown_event():
    """Clean up all clients when app shuts down"""
    for phone, client in list(clients.items()):
        try:
            await client.disconnect()
        except Exception as e:
            logger.warning(f"Error disconnecting client {phone} during shutdown: {e}")
    clients.clear()

# Retry decorator
async def retry_operation(operation, max_retries=3, initial_delay=1):
    """Retry an async operation with exponential backoff"""
    retries = 0
    while True:
        try:
            return await operation()
        except (FloodWaitError, ServerError, TimedOutError) as e:
            retries += 1
            if retries > max_retries:
                raise
            
            # Handle FloodWaitError specifically
            if isinstance(e, FloodWaitError):
                wait_time = e.seconds
                logger.warning(f"FloodWaitError: Waiting for {wait_time} seconds")
                await asyncio.sleep(wait_time)
                continue
            
            # Exponential backoff for other errors
            delay = initial_delay * (2 ** (retries - 1))
            logger.warning(f"Operation failed. Retrying in {delay}s. Error: {str(e)}")
            await asyncio.sleep(delay)
        except Exception as e:
            # Don't retry other exceptions
            logger.error(f"Non-retriable error: {str(e)}")
            raise

# API Routes
@app.get("/")
async def root():
    return {"message": "Telegram API Server is running 🚀"}

@app.post("/auth/login")
async def login(request: LoginRequest):
    phone = request.phone_number
    
    # Close any existing client to prevent database locking
    if phone in clients:
        try:
            await clients[phone].disconnect()
            del clients[phone]
        except Exception as e:
            logger.warning(f"Error disconnecting existing client: {e}")
    
    async def login_operation():
        client = await get_client(phone)
        
        if await client.is_user_authorized():
            user = await client.get_me()
            logger.info(f"User {phone} is already logged in: {user.first_name}")
            return {"message": "Already logged in", "name": user.first_name, "phone_number": phone}
        
        await client.send_code_request(phone)
        return {"message": "OTP sent", "phone_number": phone}
    
    try:
        return await retry_operation(login_operation)
    except Exception as e:
        logger.error(f"Login error for {phone}: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/auth/verify")
async def verify_otp(request: OTPVerifyRequest):
    phone, code, password = request.phone_number, request.code, request.password
    
    async def verify_operation():
        client = await get_client(phone)
        
        try:
            await client.sign_in(phone, code)
            
            if await client.is_user_authorized():
                user = await client.get_me()
                logger.info(f"User {phone} logged in successfully: {user.first_name}")
                return {"message": "Login successful", "name": user.first_name, "phone_number": phone}
            
        except SessionPasswordNeededError:
            if not password:
                raise HTTPException(status_code=401, detail="2FA password required")
            
            await client.sign_in(password=password)
            user = await client.get_me()
            logger.info(f"User {phone} logged in with 2FA: {user.first_name}")
            return {"message": "Login successful with 2FA", "name": user.first_name, "phone_number": phone}
    
    try:
        return await retry_operation(verify_operation)
    except Exception as e:
        logger.error(f"OTP verification error for {phone}: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/contacts/{phone_number}")
async def get_contacts(phone_number: str):
    logger.info(f"Fetching contacts for {phone_number}")
    
    async def contacts_operation():
        client = await get_client(phone_number)
        
        if not await client.is_user_authorized():
            raise HTTPException(status_code=403, detail="User not logged in")
        
        dialogs = await client.get_dialogs()
        contacts_list = [{"id": d.id, "name": d.title or "Unknown"} for d in dialogs if d.is_user]
        return {"contacts": contacts_list}
    
    try:
        return await retry_operation(contacts_operation)
    except Exception as e:
        logger.error(f"Error fetching contacts for {phone_number}: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/send_message")
async def send_message(request: MessageRequest):
    phone = request.phone_number
    
    async def send_operation():
        client = await get_client(phone)
        
        if not await client.is_user_authorized():
            raise HTTPException(status_code=403, detail="User not logged in")
        
        await client.send_message(request.contact_id, request.message)
        logger.info(f"Message sent to {request.contact_id} from {phone}")
        return {"message": "Message sent successfully"}
    
    try:
        return await retry_operation(send_operation)
    except Exception as e:
        logger.error(f"Message sending error for {phone}: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/messages/{phone_number}/{contact_id}")
async def get_messages(phone_number: str, contact_id: int, limit: int = 50):
    logger.info(f"Fetching messages between {phone_number} and {contact_id}")
    
    async def messages_operation():
        client = await get_client(phone_number)
        
        if not await client.is_user_authorized():
            raise HTTPException(status_code=403, detail="User not logged in")
        
        messages = []
        # Fetch messages with the specific contact
        async for message in client.iter_messages(contact_id, limit=limit):
            if message.text:  # Only include text messages
                messages.append({
                    "id": message.id,
                    "text": message.text,
                    "outgoing": message.out,
                    "timestamp": message.date.isoformat()
                })
        
        # Reverse to get oldest first
        messages.reverse()
        return {"messages": messages}
    
    try:
        return await retry_operation(messages_operation)
    except Exception as e:
        logger.error(f"Error fetching messages for {phone_number}: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/poll_messages/{phone_number}/{contact_id}")
async def poll_messages(phone_number: str, contact_id: int, last_message_id: int = 0):
    
    async def poll_operation():
        client = await get_client(phone_number)
        
        if not await client.is_user_authorized():
            raise HTTPException(status_code=403, detail="User not logged in")
        
        messages = []
        # Fetch newer messages since last_message_id
        async for message in client.iter_messages(contact_id, min_id=last_message_id):
            if message.text:  # Only include text messages
                messages.append({
                    "id": message.id,
                    "text": message.text,
                    "outgoing": message.out,
                    "timestamp": message.date.isoformat()
                })
        
        # Reverse to get oldest first
        messages.reverse()
        return {"messages": messages}
    
    try:
        return await retry_operation(poll_operation)
    except Exception as e:
        logger.error(f"Error polling messages for {phone_number}: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))