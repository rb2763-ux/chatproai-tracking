"""
ChatPro AI Email Tracking Service - Redis Version
Simple, fast tracking using Upstash Redis.
"""

import os
import json
import base64
import hashlib
from datetime import datetime
from typing import Optional
from urllib.parse import unquote

from fastapi import FastAPI, Request, Query
from fastapi.responses import RedirectResponse, Response, JSONResponse
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import httpx
import logging

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# App
app = FastAPI(
    title="ChatPro AI Email Tracking",
    description="Track email clicks and report views with Redis",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Upstash Redis config
UPSTASH_URL = os.environ.get("UPSTASH_URL", "https://infinite-gibbon-18285.upstash.io")
UPSTASH_TOKEN = os.environ.get("UPSTASH_TOKEN", "")

# 1x1 transparent GIF
TRACKING_PIXEL = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)


# ============== Redis Helpers ==============

async def redis_cmd(*args):
    """Execute Redis command via Upstash REST API."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            UPSTASH_URL,
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
            json=list(args)
        )
        data = resp.json()
        if "error" in data:
            logger.error(f"Redis error: {data['error']}")
            return None
        return data.get("result")


def generate_tracking_id(email: str, campaign: str = "default") -> str:
    """Generate unique tracking ID."""
    data = f"{email}:{campaign}:{datetime.utcnow().isoformat()}"
    return hashlib.sha256(data.encode()).hexdigest()[:16]


# ============== Campaign Management ==============

@app.post("/api/campaigns")
async def create_campaign(name: str, description: str = ""):
    """Create a new campaign."""
    campaign_id = await redis_cmd("INCR", "campaign:counter")
    campaign_data = {
        "id": campaign_id,
        "name": name,
        "description": description,
        "created_at": datetime.utcnow().isoformat()
    }
    await redis_cmd("HSET", f"campaign:{campaign_id}", "data", json.dumps(campaign_data))
    await redis_cmd("SADD", "campaigns", str(campaign_id))
    return campaign_data


@app.get("/api/campaigns")
async def list_campaigns():
    """List all campaigns."""
    campaign_ids = await redis_cmd("SMEMBERS", "campaigns") or []
    campaigns = []
    for cid in campaign_ids:
        data = await redis_cmd("HGET", f"campaign:{cid}", "data")
        if data:
            campaigns.append(json.loads(data))
    return {"campaigns": campaigns}


# ============== Email Registration ==============

@app.post("/api/emails")
async def register_email(
    campaign_id: int,
    recipient: str,
    hotel_id: str = "",
    hotel_name: str = ""
):
    """Register an email for tracking."""
    tracking_id = generate_tracking_id(recipient, str(campaign_id))
    
    email_data = {
        "tracking_id": tracking_id,
        "campaign_id": campaign_id,
        "recipient": recipient,
        "hotel_id": hotel_id,
        "hotel_name": hotel_name,
        "sent_at": datetime.utcnow().isoformat()
    }
    
    # Store email data
    await redis_cmd("HSET", f"email:{tracking_id}", "data", json.dumps(email_data))
    
    # Add to campaign's email list
    await redis_cmd("SADD", f"campaign:{campaign_id}:emails", tracking_id)
    
    # Initialize counters
    await redis_cmd("HSET", f"email:{tracking_id}", "clicks", "0")
    await redis_cmd("HSET", f"email:{tracking_id}", "opens", "0")
    await redis_cmd("HSET", f"email:{tracking_id}", "reports", "0")
    
    return {
        "tracking_id": tracking_id,
        "click_url": f"https://t.chatproai.io/c/{tracking_id}",
        "report_url": f"https://t.chatproai.io/r/{tracking_id}",
        "pixel_url": f"https://t.chatproai.io/o/{tracking_id}.gif"
    }


# ============== Tracking Endpoints ==============

@app.get("/c/{tracking_id}")
async def track_click(
    tracking_id: str,
    url: str = Query(..., description="Destination URL"),
    request: Request = None
):
    """Track link click and redirect."""
    # Increment click counter
    await redis_cmd("HINCRBY", f"email:{tracking_id}", "clicks", 1)
    
    # Log click event
    event = {
        "type": "click",
        "url": url,
        "timestamp": datetime.utcnow().isoformat(),
        "ip": request.client.host if request else None,
        "user_agent": request.headers.get("user-agent", "") if request else None
    }
    await redis_cmd("RPUSH", f"email:{tracking_id}:events", json.dumps(event))
    
    # Also track in campaign stats
    email_data = await redis_cmd("HGET", f"email:{tracking_id}", "data")
    if email_data:
        data = json.loads(email_data)
        await redis_cmd("SADD", f"campaign:{data['campaign_id']}:clickers", tracking_id)
    
    logger.info(f"Click tracked: {tracking_id} -> {url}")
    
    # Redirect to destination
    return RedirectResponse(url=unquote(url), status_code=302)


@app.get("/r/{tracking_id}")
async def track_report_view(
    tracking_id: str,
    hotel_id: str = Query(None),
    request: Request = None
):
    """Track report view and redirect to analyzer."""
    # Increment report view counter
    await redis_cmd("HINCRBY", f"email:{tracking_id}", "reports", 1)
    
    # Log event
    event = {
        "type": "report_view",
        "hotel_id": hotel_id,
        "timestamp": datetime.utcnow().isoformat(),
        "ip": request.client.host if request else None
    }
    await redis_cmd("RPUSH", f"email:{tracking_id}:events", json.dumps(event))
    
    # Track in campaign
    email_data = await redis_cmd("HGET", f"email:{tracking_id}", "data")
    if email_data:
        data = json.loads(email_data)
        await redis_cmd("SADD", f"campaign:{data['campaign_id']}:viewers", tracking_id)
    
    logger.info(f"Report view: {tracking_id}, hotel: {hotel_id}")
    
    # Redirect to analyzer
    redirect_url = f"https://analyzer.chatproai.io/report/{hotel_id}" if hotel_id else "https://analyzer.chatproai.io"
    return RedirectResponse(url=redirect_url, status_code=302)


@app.get("/o/{tracking_id}.gif")
async def track_open(tracking_id: str, request: Request = None):
    """Track email open via pixel."""
    # Increment open counter
    await redis_cmd("HINCRBY", f"email:{tracking_id}", "opens", 1)
    
    # Log event
    event = {
        "type": "open",
        "timestamp": datetime.utcnow().isoformat(),
        "ip": request.client.host if request else None
    }
    await redis_cmd("RPUSH", f"email:{tracking_id}:events", json.dumps(event))
    
    logger.info(f"Open tracked: {tracking_id}")
    
    # Return transparent pixel
    return Response(
        content=TRACKING_PIXEL,
        media_type="image/gif",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
    )


# ============== Stats Endpoints ==============

@app.get("/api/campaigns/{campaign_id}/stats")
async def get_campaign_stats(campaign_id: int):
    """Get campaign statistics."""
    # Get campaign info
    campaign_data = await redis_cmd("HGET", f"campaign:{campaign_id}", "data")
    if not campaign_data:
        return {"error": "Campaign not found"}
    
    campaign = json.loads(campaign_data)
    
    # Count emails
    emails = await redis_cmd("SMEMBERS", f"campaign:{campaign_id}:emails") or []
    clickers = await redis_cmd("SMEMBERS", f"campaign:{campaign_id}:clickers") or []
    viewers = await redis_cmd("SMEMBERS", f"campaign:{campaign_id}:viewers") or []
    
    total = len(emails)
    clicks = len(clickers)
    views = len(viewers)
    
    return {
        "campaign": campaign,
        "total_sent": total,
        "unique_clicks": clicks,
        "unique_report_views": views,
        "click_rate": round(clicks / total * 100, 2) if total > 0 else 0,
        "view_rate": round(views / total * 100, 2) if total > 0 else 0
    }


@app.get("/api/campaigns/{campaign_id}/clickers")
async def get_campaign_clickers(campaign_id: int):
    """Get list of emails that clicked."""
    clicker_ids = await redis_cmd("SMEMBERS", f"campaign:{campaign_id}:clickers") or []
    clickers = []
    
    for tid in clicker_ids:
        data = await redis_cmd("HGET", f"email:{tid}", "data")
        clicks = await redis_cmd("HGET", f"email:{tid}", "clicks") or "0"
        if data:
            email_info = json.loads(data)
            email_info["click_count"] = int(clicks)
            clickers.append(email_info)
    
    return {"clickers": clickers, "count": len(clickers)}


# ============== Health ==============

@app.get("/health")
async def health():
    """Health check."""
    # Test Redis connection
    pong = await redis_cmd("PING")
    return {
        "status": "healthy" if pong == "PONG" else "degraded",
        "redis": "connected" if pong else "disconnected",
        "timestamp": datetime.utcnow().isoformat()
    }


@app.get("/")
async def root():
    return {"service": "ChatPro AI Email Tracking", "version": "2.0.0-redis"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))


# ============== Demo Chat (AI-powered) ==============

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# Weather function for the demo bot
async def get_weather(location: str = "St. Anton am Arlberg") -> dict:
    """Fetch current weather from wttr.in."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://wttr.in/{location}?format=j1",
                timeout=5.0,
                headers={"User-Agent": "ChatProAI-Demo/1.0"}
            )
            if resp.status_code == 200:
                data = resp.json()
                current = data.get("current_condition", [{}])[0]
                return {
                    "location": location,
                    "temp_c": current.get("temp_C", "?"),
                    "feels_like_c": current.get("FeelsLikeC", "?"),
                    "condition": current.get("lang_de", [{}])[0].get("value", current.get("weatherDesc", [{}])[0].get("value", "unbekannt")),
                    "humidity": current.get("humidity", "?"),
                    "wind_kmh": current.get("windspeedKmph", "?"),
                    "snow_cm": data.get("weather", [{}])[0].get("totalSnow_cm", "0")
                }
    except Exception as e:
        logger.error(f"Weather fetch error: {e}")
    return {"error": "Wetter konnte nicht abgerufen werden"}

# Tool definitions for OpenAI
DEMO_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_weather",
            "description": "Aktuelles Wetter abrufen für St. Anton am Arlberg oder andere Orte",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "Ort für Wetterabfrage, z.B. 'St. Anton am Arlberg'"
                    }
                },
                "required": []
            }
        }
    }
]

HOTEL_SYSTEM_PROMPT = """Du bist der KI-Concierge des Berghotel Sonnblick — ein familiengeführtes 4-Sterne Hotel in St. Anton am Arlberg, Österreich.

=== DEINE PERSÖNLICHKEIT ===
Du bist warm, kompetent und aufmerksam wie ein echter Concierge. Du merkst dir Details aus dem Gespräch und nutzt sie. Du bist stolz auf dein Hotel und die Region.

=== MEHRSPRACHIGKEIT ===
Erkenne automatisch die Sprache des Gastes und antworte in derselben Sprache.
- Deutsch → Deutsch (Sie-Form)
- English → English
- Español → Español
- Français → Français
- Italiano → Italiano
Wechsle die Sprache sofort wenn der Gast wechselt.

=== HOTEL-INFORMATIONEN ===

📍 KONTAKT & LAGE:
- Adresse: Bergstraße 42, 6580 St. Anton am Arlberg, Österreich
- Telefon: +43 5446 12345 (täglich 8-22 Uhr)
- E-Mail: info@berghotel-sonnblick.demo
- Ski-in/Ski-out: Direkt an der Piste!
- 5 Min. vom Bahnhof, 100km vom Flughafen Innsbruck

🛏️ ZIMMER (alle inkl. Frühstücksbuffet):

1. Suite "Edelweiss" (45m²): €259/Nacht ⭐ BESTSELLER
   → Whirlpool, Kamin, Privatterrasse, Nespresso, Bademantel
   → "Unser beliebtestes Zimmer — Gäste lieben den privaten Whirlpool mit Bergblick"

2. Familienzimmer "Murmeltier" (38m²): €199/Nacht
   → Bis 4 Personen, Spielecke, 2 Bäder, Stockbett für Kinder
   → Kinder bis 6 Jahre GRATIS im Elternzimmer

3. Doppelzimmer "Alpenblick" (28m²): €149/Nacht
   → Balkon, Bergpanorama, Gratis WLAN, Kingsize-Bett

ANGEBOTE:
- 7 Nächte buchen = 1 Nacht GRATIS
- Frühbucher: -15% bei Buchung 60+ Tage voraus

✨ AUSSTATTUNG:
- Frühstücksbuffet (7-10:30): Regionale Bio-Produkte, frisch gebacken, hausgemachte Marmeladen
- Restaurant "Gipfelstube": Mittag 12-14h, Abend 18-21h (Tiroler Küche, vegetarisch/vegan möglich)
- Halbpension: +€35/Person/Tag verfügbar
- Wellness (15-21h, für Gäste GRATIS): Finnische Sauna, Dampfbad, Ruheraum mit Bergblick
- Massage: Auf Anfrage buchbar (€60-90)
- Ski-in/Ski-out, Skiverleih im Haus, Skischule 50m
- Gratis: Tiefgarage, E-Ladestation, High-Speed WLAN

🐕 HAUSTIERE: Hunde willkommen (€15/Nacht, Hundekorb & Napf vorhanden)

⏰ CHECK-IN/OUT: 
- Check-in: ab 15:00 (früher auf Anfrage möglich)
- Check-out: bis 11:00 (Late Checkout auf Anfrage)

❌ STORNIERUNG: Kostenlos bis 48h vor Anreise

🎿 AKTIVITÄTEN (Winter):
- 300km Pisten im Skigebiet Arlberg
- Schneeschuhwandern, Langlauf, Rodeln
- Après-Ski direkt im Ort

☀️ AKTIVITÄTEN (Sommer):
- 300km Wanderwege, Mountainbiken, Klettern
- Bergseen, Sommerrodelbahn

=== WETTER-TOOL ===
Du hast Zugriff auf LIVE-Wetterdaten! Bei Fragen zum Wetter IMMER die Funktion get_current_weather nutzen.

=== BUCHUNGS-FLOW ===
Führe den Gast direkt und schnell zur Buchung:

1. Frage nach Reisedatum und Personenzahl (kann in einer Frage sein)
2. DIREKT Verfügbarkeit bestätigen + Zimmer empfehlen + Gesamtpreis nennen
3. SOFORT zum Abschluss: "Darf ich buchen? Ich brauche nur Name und E-Mail."
4. Nach Kontaktdaten: "Gebucht! ✅ Bestätigung ist unterwegs. Wir freuen uns auf Sie!"

BEISPIEL-FLOW:
User: "15-18 Februar, 2 Personen"
→ "Die Suite Edelweiss ist verfügbar! 3 Nächte = €777 inkl. Frühstück. Soll ich buchen? Ich brauche nur Ihren Namen und E-Mail. 😊"

WICHTIG: 
- KEINE Wartezeit simulieren ("ich prüfe...", "Moment...") — antworte SOFORT
- Echter Bot mit Systemanbindung antwortet direkt, nicht "ich schaue nach"
- Gen Y/Millennials wollen SOFORTIGE Ergebnisse
- Zeige immer Gesamtpreis (Nächte × Zimmerpreis)

=== PSYCHOLOGIE & VERKAUF ===
- ANCHORING: IMMER Suite/teures Zimmer ZUERST nennen (€259), DANN günstigere. NIE "ab €149" sagen!
- SOCIAL PROOF: "Unser beliebtestes Zimmer", "Gäste lieben..."
- SCARCITY: "Im Februar/März sehr gefragt", "Ich empfehle früh zu buchen"
- PERSONALISIERUNG: Wenn du einen Namen erfährst, nutze ihn
- UPSELLING: Bei DZ-Anfrage dezent Suite erwähnen ("Für nur €110 mehr hätten Sie...")
- REZIPROZITÄT: Erst helfen (Tipps, Infos), dann zur Buchung führen

=== SONDERFÄLLE ===
- Allergien/Diäten: "Kein Problem! Unser Küchenchef bereitet gerne [X] zu. Bitte bei Buchung vermerken."
- Beschwerden: Empathisch reagieren, entschuldigen, Lösung anbieten, ggf. an Rezeption verweisen
- Gruppenanfragen (5+): "Für Gruppen erstellen wir gerne ein individuelles Angebot. Darf ich Ihre Kontaktdaten aufnehmen?"
- Preisverhandlung: "Unsere Preise sind fair kalkuliert. Aber kennen Sie schon unser 7=6 Angebot oder den Frühbucher-Rabatt?"

=== KOMMUNIKATIONSSTIL ===
- KURZ UND KNAPP (2-3 Sätze pro Antwort!)
- Warm und persönlich, nicht roboterhaft
- Max 1 Emoji pro Antwort
- Bei echten Buchungen: Begeisterung zeigen!
- Proaktiv: Biete relevante Zusatzinfos an
- Bei Unsicherheit: "Dafür verbinde ich Sie gerne mit unserer Rezeption: +43 5446 12345"

=== BEISPIEL-ANTWORTEN ===
User: "Was kostet ein Zimmer?"
→ "Unser Bestseller ist die Suite Edelweiss mit privatem Whirlpool und Kamin für €259/Nacht. Wir haben auch gemütliche Doppelzimmer ab €149. Für welches Datum suchen Sie? 😊"

User: "I'd like to book a room"
→ "Wonderful! Our most popular choice is the Edelweiss Suite with private hot tub for €259/night. We also have cozy double rooms from €149. What dates are you looking at?"

User: "Habt ihr Wellness?"
→ "Ja! Unser Wellness-Bereich mit Sauna, Dampfbad und Ruheraum ist für Hotelgäste kostenlos (15-21 Uhr). Perfekt nach einem Skitag! ❄️"
"""


class ChatRequest(BaseModel):
    message: str
    history: list = []
    systemContext: Optional[str] = None
    hotel: Optional[str] = None


@app.post("/api/demo-chat")
async def demo_chat(req: ChatRequest):
    """AI-powered hotel assistant chat with tool calling."""
    if not OPENAI_API_KEY:
        return JSONResponse({"error": "API not configured"}, status_code=500)
    
    # Use custom system context if provided, otherwise default
    system_prompt = req.systemContext if req.systemContext else HOTEL_SYSTEM_PROMPT
    messages = [{"role": "system", "content": system_prompt}]
    
    # Add recent history
    for msg in req.history[-6:]:
        messages.append({
            "role": msg.get("role", "user"),
            "content": msg.get("content", "")
        })
    
    messages.append({"role": "user", "content": req.message})
    
    try:
        async with httpx.AsyncClient() as client:
            # First call - might request tool use
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {OPENAI_API_KEY}"
                },
                json={
                    "model": "gpt-4o-mini",
                    "messages": messages,
                    "tools": DEMO_TOOLS,
                    "tool_choice": "auto",
                    "max_tokens": 300,
                    "temperature": 0.7
                },
                timeout=30.0
            )
            
            if response.status_code != 200:
                logger.error(f"OpenAI error: {response.text}")
                return JSONResponse({"error": "AI service error"}, status_code=500)
            
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]
            
            # Check if model wants to use a tool
            if message.get("tool_calls"):
                tool_call = message["tool_calls"][0]
                function_name = tool_call["function"]["name"]
                
                if function_name == "get_current_weather":
                    # Parse arguments
                    import json as json_module
                    args = json_module.loads(tool_call["function"]["arguments"])
                    location = args.get("location", "St. Anton am Arlberg")
                    
                    # Execute the function
                    weather_data = await get_weather(location)
                    
                    # Add tool response to messages
                    messages.append(message)  # assistant message with tool_calls
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": json.dumps(weather_data, ensure_ascii=False)
                    })
                    
                    # Second call with tool result
                    response2 = await client.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {OPENAI_API_KEY}"
                        },
                        json={
                            "model": "gpt-4o-mini",
                            "messages": messages,
                            "max_tokens": 300,
                            "temperature": 0.7
                        },
                        timeout=30.0
                    )
                    
                    if response2.status_code != 200:
                        logger.error(f"OpenAI error (tool response): {response2.text}")
                        return JSONResponse({"error": "AI service error"}, status_code=500)
                    
                    data2 = response2.json()
                    reply = data2["choices"][0]["message"]["content"]
                    logger.info(f"Tool call executed: {function_name} -> {weather_data}")
                    return {"reply": reply, "tool_used": function_name}
            
            # No tool call, return direct response
            reply = message.get("content", "")
            return {"reply": reply}
            
    except Exception as e:
        logger.error(f"Chat error: {e}")
        return JSONResponse({"error": "Service temporarily unavailable"}, status_code=500)


# ============== Whisper Transcription ==============

class TranscribeRequest(BaseModel):
    audio: str  # Base64 encoded audio
    format: str = "webm"  # Audio format (webm, mp3, wav, etc.)

@app.post("/api/transcribe")
async def transcribe_audio(req: TranscribeRequest):
    """Transcribe audio using OpenAI Whisper API. Auto-detects language."""
    if not OPENAI_API_KEY:
        return JSONResponse({"error": "API not configured"}, status_code=500)
    
    try:
        # Decode base64 audio
        audio_data = base64.b64decode(req.audio)
        
        # Prepare multipart form data for Whisper API
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}"
                },
                files={
                    "file": (f"audio.{req.format}", audio_data, f"audio/{req.format}")
                },
                data={
                    "model": "whisper-1"
                },
                timeout=30.0
            )
            
            if response.status_code != 200:
                logger.error(f"Whisper API error: {response.text}")
                return JSONResponse({"error": "Transcription failed"}, status_code=500)
            
            data = response.json()
            transcript = data.get("text", "")
            
            logger.info(f"Transcribed: {transcript[:50]}...")
            return {"transcript": transcript}
            
    except Exception as e:
        logger.error(f"Transcription error: {e}")
        return JSONResponse({"error": "Transcription service unavailable"}, status_code=500)


# ============== Guest House Holland Chat ==============

GHH_SYSTEM_PROMPT = """You are Dre's AI assistant for Guest House Holland — your job is to help guests find the perfect apartment or tour in Juan Dolio, Dominican Republic, and guide them to booking.

## CRITICAL: The owner's name is DRE (not André, not Andre). ALWAYS say "Dre", NEVER "André".

## Your Role:
- Answer questions about apartments, prices, tours, and services
- Be warm, helpful, and professional — you represent Dre personally
- Detect the guest's language and respond in that language (EN/DE/NL/ES/FR/PT/IT supported)
- If you don't know a specific price, say "Let me check with Dre for the exact price"
- BE PROACTIVE: When someone shows interest, guide them toward booking!

## BOOKING FLOW (IMPORTANT!):
1. Answer questions helpfully
2. When interest is shown → ASK: "Great choice! When are you planning to visit and how many guests?"
3. Collect: travel dates, number of guests, preferences/budget
4. Confirm: "So that's [apartment] for [X] guests from [date] to [date], correct?"
5. Close: "Perfect! I'll pass this to Dre and he'll confirm availability shortly. What's your email?"
6. DO NOT just redirect to WhatsApp — complete the conversation HERE!

## About Guest House Holland:
- Owner: Dre Broeders - Dutch host, 30+ years in DR
- First official ECO-Tourguide of the Dominican Republic (#001)
- Speaks: Dutch, German, English, Spanish, French
- Booking.com Traveller Review Award since 2020
- Website: guesthouseholland.com

## Apartments (with confirmed prices):
**Premium:** Solano ($90/night, private pool), Valentine Tower A3 ($1,300/month)
**Beach:** Antoinetta 2 ($50/night), M6 ($45/night), M3 ($36/night), MM103 ($42/night), M1 ($36/night), Michael Beach House ($32/night)
**Budget:** Luis2 ($30/night), Giselly 10 ($35/night), Beach & Center ($38/night)
**Long-term:** M7 ($530/month), Studio Center ($350/month), M6 ($600/month), M3 ($550/month)
**30+ apartments total - for more options, contact Dre**

## Tours (prices depend on group size & pickup):
- Los Haitises National Park (caves, mangroves, birds)
- Whale Watching (January-March, Samaná)
- Santo Domingo (Colonial Zone, first European city)
- Saona Island (paradise beach, starfish)
- Private/custom tours available

## Taxi & Transfers:
**IMPORTANT:** For any taxi/transfer price questions → DIRECT to WhatsApp!
Say: "For transfer prices, please contact Dre directly on WhatsApp: +1 809 399 5766 — he offers individual prices based on your route, group size, and schedule."
DO NOT quote prices or say "let me check with Dre" — just give the WhatsApp link!
Services: Airport transfers (SDQ, PUJ, STI), hotel pickups, day trips, multi-day tours

## Testimonials:
- "12 years as a guest - always reliable" (Lars)
- "Safe and professional, very knowledgeable" (Sean)
- "Felt like an old friend" (Dutch family)

## Response Style:
- Keep responses concise (2-4 sentences)
- Use emojis sparingly 🌴
- Be enthusiastic about the Dominican Republic
- For tour prices: always mention they depend on group size and pickup location
"""

# In-memory conversation storage for GHH
ghh_conversations = {}

class GHHChatRequest(BaseModel):
    message: str
    conversationId: Optional[str] = None
    language: str = "en"

# Function for sending booking requests to Dre
GHH_FUNCTIONS = [
    {
        "name": "send_booking_request",
        "description": "Send a booking request to Dre when guest has provided: apartment name, check-in date, check-out date or duration, number of guests, and contact info (email or phone). Call this function to notify Dre about the inquiry.",
        "parameters": {
            "type": "object",
            "properties": {
                "apartment": {"type": "string", "description": "Name of the apartment"},
                "check_in": {"type": "string", "description": "Check-in date"},
                "check_out": {"type": "string", "description": "Check-out date or duration"},
                "guests": {"type": "string", "description": "Number of guests"},
                "contact": {"type": "string", "description": "Guest email or phone number"},
                "name": {"type": "string", "description": "Guest name if provided"},
                "notes": {"type": "string", "description": "Any special requests or notes"}
            },
            "required": ["apartment", "check_in", "guests", "contact"]
        }
    }
]

async def send_andre_notification(booking_data: dict) -> bool:
    """Send booking notification email to Dre via AWS SES."""
    try:
        import boto3
        from botocore.config import Config
        
        ses = boto3.client(
            'ses',
            region_name='eu-central-1',
            config=Config(connect_timeout=5, read_timeout=10)
        )
        
        subject = f"🏠 New Booking Request: {booking_data.get('apartment', 'Unknown')}"
        
        body = f"""Hi Dre,

You have a new booking inquiry from the website chatbot!

📍 Apartment: {booking_data.get('apartment', 'Not specified')}
📅 Check-in: {booking_data.get('check_in', 'Not specified')}
📅 Check-out: {booking_data.get('check_out', 'Not specified')}
👥 Guests: {booking_data.get('guests', 'Not specified')}
👤 Name: {booking_data.get('name', 'Not provided')}
📧 Contact: {booking_data.get('contact', 'Not provided')}

Notes: {booking_data.get('notes', 'None')}

---
Please check availability and contact the guest!

Best,
Your ChatBot 🤖
"""
        
        ses.send_email(
            Source='info@chatproai.io',
            Destination={'ToAddresses': ['drebroeders@gmail.com']},
            Message={
                'Subject': {'Data': subject, 'Charset': 'UTF-8'},
                'Body': {'Text': {'Data': body, 'Charset': 'UTF-8'}}
            }
        )
        logger.info(f"Sent booking notification to Dre: {booking_data.get('apartment')}")
        return True
    except Exception as e:
        logger.error(f"Failed to send Dre notification: {e}")
        return False

@app.post("/api/ghh-chat")
async def ghh_chat(req: GHHChatRequest):
    """AI-powered chat for Guest House Holland."""
    if not OPENAI_API_KEY:
        return JSONResponse({"error": "API not configured"}, status_code=500)
    
    import uuid
    conv_id = req.conversationId or str(uuid.uuid4())
    
    if conv_id not in ghh_conversations:
        ghh_conversations[conv_id] = [{"role": "system", "content": GHH_SYSTEM_PROMPT}]
    
    # Add user message
    ghh_conversations[conv_id].append({"role": "user", "content": req.message})
    
    # Keep conversation manageable
    if len(ghh_conversations[conv_id]) > 21:
        ghh_conversations[conv_id] = [ghh_conversations[conv_id][0]] + ghh_conversations[conv_id][-20:]
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {OPENAI_API_KEY}"
                },
                json={
                    "model": "gpt-4o-mini",
                    "messages": ghh_conversations[conv_id],
                    "max_tokens": 400,
                    "temperature": 0.7,
                    "functions": GHH_FUNCTIONS,
                    "function_call": "auto"
                },
                timeout=30.0
            )
            
            if response.status_code != 200:
                logger.error(f"OpenAI error: {response.text}")
                return JSONResponse({"error": "AI service error"}, status_code=500)
            
            data = response.json()
            choice = data["choices"][0]
            
            # Check if function was called
            if choice.get("finish_reason") == "function_call" or choice["message"].get("function_call"):
                func_call = choice["message"]["function_call"]
                if func_call["name"] == "send_booking_request":
                    booking_data = json.loads(func_call["arguments"])
                    email_sent = await send_andre_notification(booking_data)
                    
                    # Add function result to conversation
                    ghh_conversations[conv_id].append(choice["message"])
                    ghh_conversations[conv_id].append({
                        "role": "function",
                        "name": "send_booking_request",
                        "content": json.dumps({"success": email_sent})
                    })
                    
                    # Get final response from GPT
                    response2 = await client.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {OPENAI_API_KEY}"
                        },
                        json={
                            "model": "gpt-4o-mini",
                            "messages": ghh_conversations[conv_id],
                            "max_tokens": 200,
                            "temperature": 0.7
                        },
                        timeout=30.0
                    )
                    data2 = response2.json()
                    reply = data2["choices"][0]["message"]["content"]
                    ghh_conversations[conv_id].append({"role": "assistant", "content": reply})
                    logger.info(f"GHH Booking sent for: {booking_data.get('apartment')}")
                    return {"reply": reply, "conversationId": conv_id, "bookingSent": True}
            
            reply = choice["message"]["content"]
            ghh_conversations[conv_id].append({"role": "assistant", "content": reply})
            
            logger.info(f"GHH Chat: {req.message[:50]}... -> {reply[:50]}...")
            return {"reply": reply, "conversationId": conv_id}
            
    except Exception as e:
        logger.error(f"GHH Chat error: {e}")
        return JSONResponse({"error": "Service temporarily unavailable"}, status_code=500)
