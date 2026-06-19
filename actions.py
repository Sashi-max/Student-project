from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Text

import requests
from rasa_sdk import Action, FormValidationAction, Tracker
from rasa_sdk.events import EventType, FollowupAction, SlotSet
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.types import DomainDict

LOGGER = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

CLIMATIQ_ENDPOINT = "https://api.climatiq.io/v1/estimate"
AMADEUS_BASE_URL = "https://test.api.amadeus.com"
DEFAULT_TELEGRAM_TOKEN = "8981649675:AAEQVOEwrSP6HV9mzmeMW9c_yLU9M7uKM0o"
TELEGRAM_SEND_ENDPOINT = "https://api.telegram.org/bot{token}/sendMessage"

CITY_CODES = {
    "amsterdam": "AMS",
    "barcelona": "BCN",
    "berlin": "BER",
    "copenhagen": "CPH",
    "costa rica": "SJO",
    "lisbon": "LIS",
    "london": "LON",
    "manchester": "MAN",
    "new york": "NYC",
    "paris": "PAR",
    "rome": "ROM",
}

DEMO_CARBON = {
    "rail": {"kg_co2e": 22.4, "label": "green", "source": "cached_demo"},
    "coach": {"kg_co2e": 31.7, "label": "green", "source": "cached_demo"},
    "flight": {"kg_co2e": 168.9, "label": "red", "source": "cached_demo"},
}

DEMO_TRAVEL_OPTIONS = {
    "flights": [
        {
            "id": "flight-demo-1",
            "name": "Direct economy flight",
            "mode": "flight",
            "price": 235.0,
            "currency": "GBP",
            "provider": "Amadeus cached demo",
        },
        {
            "id": "rail-demo-1",
            "name": "Rail-first route",
            "mode": "rail",
            "price": 185.0,
            "currency": "GBP",
            "provider": "Curated cached demo",
        },
    ],
    "hotels": [
        {
            "id": "hotel-demo-1",
            "name": "Canal Eco Lodge",
            "price": 118.0,
            "currency": "GBP",
            "rating": 4.5,
            "sustainability_features": ["renewable energy", "water refill points", "local hiring"],
        },
        {
            "id": "hotel-demo-2",
            "name": "Neighbourhood Green Hotel",
            "price": 96.0,
            "currency": "GBP",
            "rating": 4.2,
            "sustainability_features": ["bike rental", "low-waste breakfast", "public transport pass"],
        },
    ],
    "experiences": [
        {
            "name": "Community-led food walk",
            "impact_note": "Supports local guides and small food businesses.",
        },
        {
            "name": "Car-free heritage route",
            "impact_note": "Designed around walking, cycling, and public transport.",
        },
    ],
}


def _slot(tracker: Tracker, name: str, default: Any = None) -> Any:
    value = tracker.get_slot(name)
    return default if value in (None, "") else value


def _city_code(city: Optional[str], default: str) -> str:
    if not city:
        return default
    return CITY_CODES.get(str(city).strip().lower(), str(city).strip()[:3].upper())


def _carbon_label(kg_co2e: float) -> str:
    if kg_co2e < 50:
        return "green"
    if kg_co2e <= 150:
        return "amber"
    return "red"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if isinstance(value, str):
            match = re.search(r"\d+(?:\.\d+)?", value.replace(",", ""))
            return float(match.group(0)) if match else default
        return float(value)
    except (TypeError, ValueError):
        return default


def _valid_date(value: Any) -> bool:
    try:
        datetime.strptime(str(value), "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def _preference_weights(preference: Optional[str]) -> Dict[str, float]:
    preference = (preference or "medium").lower()
    if preference == "high":
        return {"sustainability": 0.75, "price": 0.25}
    if preference == "low":
        return {"sustainability": 0.35, "price": 0.65}
    return {"sustainability": 0.55, "price": 0.45}


def _summarise_events(tracker: Tracker, limit: int = 12) -> str:
    lines: List[str] = []
    for event in tracker.events[-limit:]:
        if event.get("event") == "user":
            text = event.get("text", "")
            if text:
                lines.append(f"User: {text}")
        if event.get("event") == "bot":
            text = event.get("text", "")
            if text:
                lines.append(f"Bot: {text}")
    return "\n".join(lines[-limit:]) or "No recent message history available."


def _missing_trip_slots(tracker: Tracker) -> List[str]:
    required = ["destination", "start_date", "end_date", "budget", "sustainability_preference"]
    return [slot for slot in required if not _slot(tracker, slot)]


def _format_carbon(carbon_results: Optional[Dict[str, Dict[str, Any]]]) -> str:
    if not carbon_results:
        return "No carbon estimate has been calculated yet."
    return "\n".join(
        f"- {mode}: {data.get('kg_co2e', 'n/a')} kg CO2e ({data.get('label', 'unlabelled')}, source: {data.get('source', 'unknown')})"
        for mode, data in carbon_results.items()
    )


def _format_itinerary(ranked_results: Optional[Dict[str, Any]]) -> str:
    if not ranked_results:
        return "No ranked recommendation has been generated yet."
    transport = ranked_results.get("transport", [])
    hotels = ranked_results.get("hotels", [])
    experiences = ranked_results.get("experiences", [])
    lines = ["Recommended itinerary:"]
    if transport:
        first = transport[0]
        lines.append(
            f"- Transport: {first.get('name')} ({first.get('mode', 'unknown')}, {first.get('carbon_kg', 'n/a')} kg CO2e, score {first.get('score', 'n/a')})"
        )
    if hotels:
        first = hotels[0]
        lines.append(
            f"- Hotel: {first.get('name')} ({first.get('currency', 'GBP')} {first.get('price', 'n/a')}/night, label {first.get('carbon_label', 'amber')}, score {first.get('score', 'n/a')})"
        )
    for experience in experiences[:2]:
        lines.append(f"- Experience: {experience.get('name')} - {experience.get('impact_note')}")
    return "\n".join(lines)


# def _telegram_chat_id(tracker: Tracker) -> Optional[str]:
#     configured = (
#         os.getenv("TELEGRAM_ADMIN_CHAT_ID")
#         or os.getenv("TELEGRAM_CHAT_ID")
#         or os.getenv("TELEGRAM_DEFAULT_CHAT_ID")
#     )
#     if configured:
#         return configured.strip()
#     sender_id = str(tracker.sender_id or "").strip()
#     return sender_id if re.fullmatch(r"-?\d+", sender_id) else None

def _telegram_chat_id(tracker: Tracker) -> Optional[str]:
    # Always try environment first
    configured = (
        os.getenv("TELEGRAM_ADMIN_CHAT_ID")
        or os.getenv("TELEGRAM_CHAT_ID")
    )

    if configured and configured.strip():
        return configured.strip()

    return None

class ValidateTripPlanningForm(FormValidationAction):
    def name(self) -> Text:
        return "validate_trip_planning_form"

    async def validate_destination(
        self,
        slot_value: Any,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: DomainDict,
    ) -> Dict[Text, Any]:
        destination = str(slot_value).strip() if slot_value else ""
        if len(destination) < 2 or destination.lower() in {"somewhere", "anywhere"}:
            dispatcher.utter_message(text="Please share a specific city or region so I can compare real options.")
            return {"destination": None}
        return {"destination": destination.title()}

    async def validate_start_date(
        self,
        slot_value: Any,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: DomainDict,
    ) -> Dict[Text, Any]:
        if not _valid_date(slot_value):
            dispatcher.utter_message(text="Please give the departure date in YYYY-MM-DD format.")
            return {"start_date": None}
        return {"start_date": str(slot_value)}

    async def validate_end_date(
        self,
        slot_value: Any,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: DomainDict,
    ) -> Dict[Text, Any]:
        if not _valid_date(slot_value):
            dispatcher.utter_message(text="Please give the return date in YYYY-MM-DD format.")
            return {"end_date": None}
        start_date = tracker.get_slot("start_date")
        if start_date and _valid_date(start_date):
            if datetime.strptime(str(slot_value), "%Y-%m-%d") < datetime.strptime(str(start_date), "%Y-%m-%d"):
                dispatcher.utter_message(text="The return date must be after the departure date.")
                return {"end_date": None}
        return {"end_date": str(slot_value)}

    async def validate_budget(
        self,
        slot_value: Any,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: DomainDict,
    ) -> Dict[Text, Any]:
        budget = _safe_float(slot_value)
        if budget < 50:
            dispatcher.utter_message(text="Please provide a realistic total budget above 50.")
            return {"budget": None}
        return {"budget": budget}

    async def validate_sustainability_preference(
        self,
        slot_value: Any,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: DomainDict,
    ) -> Dict[Text, Any]:
        value = str(slot_value).lower().strip()
        synonyms = {
            "very eco friendly": "high",
            "lowest carbon possible": "high",
            "maximum sustainability": "high",
            "balanced": "medium",
            "balance cost and carbon": "medium",
            "cheaper is more important": "low",
            "cost first": "low",
        }
        value = synonyms.get(value, value)
        if value not in {"low", "medium", "high"}:
            dispatcher.utter_message(
                text="Please choose low, medium, or high sustainability priority.",
                buttons=[
                    {"title": "Low", "payload": '/inform_sustainability{"sustainability_preference":"low"}'},
                    {"title": "Medium", "payload": '/inform_sustainability{"sustainability_preference":"medium"}'},
                    {"title": "High", "payload": '/inform_sustainability{"sustainability_preference":"high"}'},
                ],
            )
            return {"sustainability_preference": None}
        return {"sustainability_preference": value}


class ActionCarbonCalculator(Action):
    def name(self) -> Text:
        return "action_calculate_carbon"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[EventType]:
        destination = _slot(tracker, "destination")
        origin = _slot(tracker, "origin", "London")
        if not destination:
            dispatcher.utter_message(text="I need a destination before calculating carbon.")
            return [FollowupAction("trip_planning_form")]

        api_key = os.getenv("CLIMATIQ_API_KEY")
        results: Dict[str, Dict[str, Any]] = {}
        try:
            if not api_key:
                raise RuntimeError("CLIMATIQ_API_KEY is not configured")

            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            estimates = {
                "rail": {"activity_id": "passenger_train-route_type_international", "distance_km": 520},
                "coach": {"activity_id": "passenger_vehicle-vehicle_type_bus-fuel_source_na", "distance_km": 520},
                "flight": {"activity_id": "passenger_flight-route_type_international-distance_na-class_economy", "distance_km": 620},
            }
            for mode, estimate in estimates.items():
                payload = {
                    "emission_factor": {"activity_id": estimate["activity_id"], "data_version": "^21"},
                    "parameters": {"distance": estimate["distance_km"], "distance_unit": "km"},
                }
                response = requests.post(CLIMATIQ_ENDPOINT, json=payload, headers=headers, timeout=10)
                response.raise_for_status()
                kg = float(response.json().get("co2e", 0.0))
                results[mode] = {"kg_co2e": round(kg, 1), "label": _carbon_label(kg), "source": "climatiq"}
        except Exception as exc:
            LOGGER.exception("Carbon API failed; using cached demo data: %s", exc)
            results = DEMO_CARBON

        summary = ", ".join(f"{mode}: {data['kg_co2e']} kg CO2e ({data['label']})" for mode, data in results.items())
        dispatcher.utter_message(
            text=f"Estimated one-way carbon from {origin} to {destination}: {summary}. These are estimates, not absolute green claims.",
            json_message={"type": "carbon_summary", "results": results},
        )
        return [SlotSet("carbon_results", results)]


class ActionCarbonCalculatorLegacy(ActionCarbonCalculator):
    def name(self) -> Text:
        return "action_carbon_calculator"


class AmadeusClient:
    def __init__(self) -> None:
        self.client_id = os.getenv("AMADEUS_CLIENT_ID")
        self.client_secret = os.getenv("AMADEUS_CLIENT_SECRET")
        self.base_url = os.getenv("AMADEUS_BASE_URL", AMADEUS_BASE_URL)

    def token(self) -> str:
        if not self.client_id or not self.client_secret:
            raise RuntimeError("Amadeus credentials are not configured")
        response = requests.post(
            f"{self.base_url}/v1/security/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=10,
        )
        response.raise_for_status()
        return response.json()["access_token"]

    def fetch(self, origin: str, destination: str, start_date: str, adults: int) -> Dict[str, List[Dict[str, Any]]]:
        token = self.token()
        headers = {"Authorization": f"Bearer {token}"}
        origin_code = _city_code(origin, "LON")
        destination_code = _city_code(destination, "AMS")
        flights_response = requests.get(
            f"{self.base_url}/v2/shopping/flight-offers",
            headers=headers,
            params={
                "originLocationCode": origin_code,
                "destinationLocationCode": destination_code,
                "departureDate": start_date,
                "adults": adults,
                "max": 5,
                "currencyCode": "GBP",
            },
            timeout=12,
        )
        flights_response.raise_for_status()
        flights = []
        for offer in flights_response.json().get("data", [])[:5]:
            price = _safe_float(offer.get("price", {}).get("grandTotal"))
            flights.append(
                {
                    "id": offer.get("id", "flight"),
                    "name": f"Flight option {offer.get('id', '')}".strip(),
                    "mode": "flight",
                    "price": price,
                    "currency": offer.get("price", {}).get("currency", "GBP"),
                    "provider": "Amadeus",
                }
            )

        hotels = DEMO_TRAVEL_OPTIONS["hotels"]
        experiences = DEMO_TRAVEL_OPTIONS["experiences"]
        return {"flights": flights or DEMO_TRAVEL_OPTIONS["flights"], "hotels": hotels, "experiences": experiences}


class ActionFetchTravelOptions(Action):
    def name(self) -> Text:
        return "action_fetch_travel_options"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[EventType]:
        destination = _slot(tracker, "destination")
        start_date = _slot(tracker, "start_date")
        origin = _slot(tracker, "origin", "London")
        adults = int(_safe_float(_slot(tracker, "people", 1), 1))

        if not destination or not start_date:
            dispatcher.utter_message(text="I need the destination and departure date before searching travel options.")
            return [FollowupAction("trip_planning_form")]

        try:
            options = AmadeusClient().fetch(origin, destination, start_date, adults)
        except Exception as exc:
            LOGGER.exception("Amadeus API failed; using cached demo data: %s", exc)
            options = DEMO_TRAVEL_OPTIONS

        dispatcher.utter_message(
            text=f"I found {len(options['hotels'])} hotel options, {len(options['flights'])} transport options, and {len(options['experiences'])} cultural experiences.",
            json_message={"type": "travel_options", "options": options},
        )
        return [SlotSet("travel_options", options)]


class ActionRankResults(Action):
    def name(self) -> Text:
        return "action_rank_results"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[EventType]:
        carbon_results = _slot(tracker, "carbon_results", DEMO_CARBON)
        travel_options = _slot(tracker, "travel_options", DEMO_TRAVEL_OPTIONS)
        preference = _slot(tracker, "sustainability_preference", "medium")
        weights = _preference_weights(preference)

        try:
            transport_options = travel_options.get("flights", [])
            hotel_options = travel_options.get("hotels", [])
            all_prices = [max(_safe_float(item.get("price")), 1.0) for item in transport_options + hotel_options]
            max_price = max(all_prices) if all_prices else 1.0
            max_carbon = max([_safe_float(item.get("kg_co2e")) for item in carbon_results.values()] or [1.0])

            ranked_transport = []
            for item in transport_options:
                mode = item.get("mode", "flight")
                carbon_kg = _safe_float(carbon_results.get(mode, carbon_results.get("flight", {})).get("kg_co2e"), 150.0)
                carbon_score = 1 - min(carbon_kg / max(max_carbon, 1.0), 1)
                price_score = 1 - min(_safe_float(item.get("price")) / max(max_price, 1.0), 1)
                score = (weights["sustainability"] * carbon_score) + (weights["price"] * price_score)
                ranked_transport.append({**item, "carbon_kg": carbon_kg, "carbon_label": _carbon_label(carbon_kg), "score": round(score, 3)})

            ranked_hotels = []
            for hotel in hotel_options:
                features = hotel.get("sustainability_features", [])
                carbon_score = min(len(features) / 4, 1)
                price_score = 1 - min(_safe_float(hotel.get("price")) / max(max_price, 1.0), 1)
                score = (weights["sustainability"] * carbon_score) + (weights["price"] * price_score)
                ranked_hotels.append({**hotel, "carbon_label": "green" if carbon_score >= 0.5 else "amber", "score": round(score, 3)})

            ranked_transport.sort(key=lambda item: item["score"], reverse=True)
            ranked_hotels.sort(key=lambda item: item["score"], reverse=True)
            ranked = {
                "weights": weights,
                "transport": ranked_transport,
                "hotels": ranked_hotels,
                "experiences": travel_options.get("experiences", []),
            }
        except Exception as exc:
            LOGGER.exception("Ranking failed; using safe demo ranking: %s", exc)
            ranked = {"weights": weights, "transport": DEMO_TRAVEL_OPTIONS["flights"], "hotels": DEMO_TRAVEL_OPTIONS["hotels"], "experiences": DEMO_TRAVEL_OPTIONS["experiences"]}

        best_transport = ranked["transport"][0]["name"] if ranked["transport"] else "No transport option"
        best_hotel = ranked["hotels"][0]["name"] if ranked["hotels"] else "No hotel option"
        dispatcher.utter_message(
            text=f"Top recommendation: {best_transport} with {best_hotel}. I weighted sustainability at {weights['sustainability']:.0%} and price at {weights['price']:.0%}.",
            json_message={"type": "ranked_results", "results": ranked},
        )
        for hotel in ranked["hotels"][:2]:
            dispatcher.utter_message(
                text=f"{hotel['name']} - {hotel.get('currency', 'GBP')} {hotel.get('price')} per night - carbon label {hotel.get('carbon_label', 'amber')}",
                json_message={"type": "hotel_card", "hotel": hotel},
            )
        return [SlotSet("ranked_results", ranked)]


class ActionGetRecommendations(Action):
    def name(self) -> Text:
        return "action_get_recommendations"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[EventType]:
        missing = _missing_trip_slots(tracker)
        if missing:
            readable = ", ".join(slot.replace("_", " ") for slot in missing)
            dispatcher.utter_message(
                text=f"I need a little more trip context before recommending options: {readable}.",
                buttons=[
                    {"title": "Complete trip details", "payload": "/book_trip"},
                    {"title": "Human help", "payload": "/ask_handover"},
                ],
            )
            return [FollowupAction("trip_planning_form")]

        destination = _slot(tracker, "destination")
        start_date = _slot(tracker, "start_date")
        origin = _slot(tracker, "origin", "London")
        adults = int(_safe_float(_slot(tracker, "people", 1), 1))
        preference = _slot(tracker, "sustainability_preference", "medium")
        events: List[EventType] = []

        carbon_results = _slot(tracker, "carbon_results")
        if not carbon_results:
            carbon_results = DEMO_CARBON
            events.append(SlotSet("carbon_results", carbon_results))

        try:
            travel_options = AmadeusClient().fetch(origin, destination, start_date, adults)
        except Exception as exc:
            LOGGER.exception("Recommendation travel API failed; using cached demo data: %s", exc)
            travel_options = DEMO_TRAVEL_OPTIONS

        ranked = self._rank(carbon_results, travel_options, preference)
        events.extend([SlotSet("travel_options", travel_options), SlotSet("ranked_results", ranked)])

        weights = ranked["weights"]
        best_transport = ranked["transport"][0] if ranked["transport"] else {}
        best_hotel = ranked["hotels"][0] if ranked["hotels"] else {}
        dispatcher.utter_message(
            text=(
                f"Recommended itinerary for {destination}: {best_transport.get('name', 'transport option')} "
                f"plus {best_hotel.get('name', 'hotel option')}. "
                f"Ranking weights: sustainability {weights['sustainability']:.0%}, price {weights['price']:.0%}."
            ),
            json_message={"type": "ranked_results", "results": ranked},
        )
        dispatcher.utter_message(
            text=(
                "Carbon labels: green is under 50 kg CO2e, amber is 50-150 kg CO2e, "
                "and red is above 150 kg CO2e. Values are estimates and should be treated as decision support."
            )
        )
        for hotel in ranked["hotels"][:2]:
            dispatcher.utter_message(
                text=f"{hotel['name']} - {hotel.get('currency', 'GBP')} {hotel.get('price')} per night - carbon label {hotel.get('carbon_label', 'amber')}",
                json_message={"type": "hotel_card", "hotel": hotel},
                buttons=[
                    {"title": "Carbon details", "payload": "/ask_carbon"},
                    {"title": "Human advisor", "payload": "/ask_handover"},
                ],
            )
        return events

    @staticmethod
    def _rank(
        carbon_results: Dict[str, Dict[str, Any]],
        travel_options: Dict[str, List[Dict[str, Any]]],
        preference: str,
    ) -> Dict[str, Any]:
        weights = _preference_weights(preference)
        try:
            transport_options = travel_options.get("flights", [])
            hotel_options = travel_options.get("hotels", [])
            all_prices = [max(_safe_float(item.get("price")), 1.0) for item in transport_options + hotel_options]
            max_price = max(all_prices) if all_prices else 1.0
            max_carbon = max([_safe_float(item.get("kg_co2e")) for item in carbon_results.values()] or [1.0])

            ranked_transport = []
            for item in transport_options:
                mode = item.get("mode", "flight")
                carbon_kg = _safe_float(carbon_results.get(mode, carbon_results.get("flight", {})).get("kg_co2e"), 150.0)
                carbon_score = 1 - min(carbon_kg / max(max_carbon, 1.0), 1)
                price_score = 1 - min(_safe_float(item.get("price")) / max(max_price, 1.0), 1)
                score = (weights["sustainability"] * carbon_score) + (weights["price"] * price_score)
                ranked_transport.append({**item, "carbon_kg": carbon_kg, "carbon_label": _carbon_label(carbon_kg), "score": round(score, 3)})

            ranked_hotels = []
            for hotel in hotel_options:
                features = hotel.get("sustainability_features", [])
                carbon_score = min(len(features) / 4, 1)
                price_score = 1 - min(_safe_float(hotel.get("price")) / max(max_price, 1.0), 1)
                score = (weights["sustainability"] * carbon_score) + (weights["price"] * price_score)
                ranked_hotels.append({**hotel, "carbon_label": "green" if carbon_score >= 0.5 else "amber", "score": round(score, 3)})

            ranked_transport.sort(key=lambda item: item["score"], reverse=True)
            ranked_hotels.sort(key=lambda item: item["score"], reverse=True)
            return {
                "weights": weights,
                "transport": ranked_transport,
                "hotels": ranked_hotels,
                "experiences": travel_options.get("experiences", []),
            }
        except Exception as exc:
            LOGGER.exception("Recommendation ranking failed; using cached ranking: %s", exc)
            return {
                "weights": weights,
                "transport": DEMO_TRAVEL_OPTIONS["flights"],
                "hotels": DEMO_TRAVEL_OPTIONS["hotels"],
                "experiences": DEMO_TRAVEL_OPTIONS["experiences"],
            }


# class ActionHandoverToHuman(Action):
#     def name(self) -> Text:
#         return "action_handover_to_human"

#     def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[EventType]:
#         summary = self._build_summary(tracker)
#         token = os.getenv("TELEGRAM_BOT_TOKEN", DEFAULT_TELEGRAM_TOKEN)
#         chat_id = _telegram_chat_id(tracker)

#         try:
#             if not token:
#                 raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
#             if not chat_id:
#                 raise RuntimeError(
#                     "No Telegram chat id found. Set TELEGRAM_ADMIN_CHAT_ID, TELEGRAM_CHAT_ID, or use Telegram channel sender_id."
#                 )
#             self._send_with_retry(token=token, chat_id=chat_id, text=summary)
#             dispatcher.utter_message(
#                 text="A human advisor has been sent your trip summary, including dates, budget, carbon estimate, and recommendations."
#             )
#         except Exception as exc:
#             LOGGER.exception("Telegram handover failed: %s", exc)
#             dispatcher.utter_message(
#                 text=(
#                     "I prepared the handover summary, but could not send it to Telegram. "
#                     "Please check TELEGRAM_ADMIN_CHAT_ID and network access. I am showing the summary here so it is not lost."
#                 ),
#                 json_message={"type": "handover_summary", "summary": summary},
#             )
#         return [SlotSet("conversation_summary", summary)]
class ActionHandoverToHuman(Action):
    def name(self) -> Text:
        return "action_handover_to_human"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: DomainDict
    ) -> List[EventType]:

        summary = self._build_summary(tracker)

        # Telegram token (optional)
        token = os.getenv("TELEGRAM_BOT_TOKEN") or DEFAULT_TELEGRAM_TOKEN

        # Chat id (optional)
        chat_id = _telegram_chat_id(tracker)

 

        if not token:
            dispatcher.utter_message(
                text="Telegram token not configured. Human handover skipped."
            )
            dispatcher.utter_message(
                json_message={"type": "handover_summary", "summary": summary}
            )
            return [SlotSet("conversation_summary", summary)]

        if not chat_id:
            dispatcher.utter_message(
                text=(
                    "Showing summary instead."
                )
            )
            dispatcher.utter_message(
                json_message={"type": "handover_summary", "summary": summary}
            )
            return [SlotSet("conversation_summary", summary)]

        # -----------------------------
        # TRY TELEGRAM SEND (ONLY IF CONFIGURED)
        # -----------------------------
        try:
            self._send_with_retry(
                token=token,
                chat_id=chat_id,
                text=summary
            )

            dispatcher.utter_message(
                text=(
                    "A human advisor has been sent your trip summary, including dates, "
                    "budget, carbon estimate, and recommendations."
                )
            )

        except Exception as exc:
            LOGGER.exception("Telegram handover failed: %s", exc)

            dispatcher.utter_message(
                text=(
                    "Telegram handover failed. Showing summary locally instead."
                ),
                json_message={"type": "handover_summary", "summary": summary},
            )

        return [SlotSet("conversation_summary", summary)]

    @staticmethod
    def _send_with_retry(token: str, chat_id: str, text: str, attempts: int = 3) -> None:
        last_error: Optional[Exception] = None
        endpoint = TELEGRAM_SEND_ENDPOINT.format(token=token)
        for attempt in range(1, attempts + 1):
            try:
                response = requests.post(
                    endpoint,
                    json={
                        "chat_id": chat_id,
                        "text": text[:3900],
                        "disable_web_page_preview": True,
                    },
                    timeout=12,
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"Telegram API {response.status_code}: {response.text[:300]}")
                return
            except Exception as exc:
                last_error = exc
                LOGGER.warning("Telegram handover attempt %s/%s failed: %s", attempt, attempts, exc)
                if attempt < attempts:
                    time.sleep(1.5 * attempt)
        raise RuntimeError(f"Telegram handover failed after {attempts} attempts: {last_error}")

    @staticmethod
    def _build_summary(tracker: Tracker) -> str:
        destination = tracker.get_slot("destination") or "not provided"
        start_date = tracker.get_slot("start_date") or "not provided"
        end_date = tracker.get_slot("end_date") or "not provided"
        budget = tracker.get_slot("budget") or "not provided"
        preference = tracker.get_slot("sustainability_preference") or "not provided"
        carbon_results = tracker.get_slot("carbon_results")
        ranked_results = tracker.get_slot("ranked_results")

        return (
            "Eco-Travel Advisor human handover\n\n"
            f"User ID: {tracker.sender_id}\n"
            f"Destination: {destination}\n"
            f"Dates: {start_date} to {end_date}\n"
            f"Budget: {budget}\n"
            f"Sustainability preference: {preference}\n\n"
            "Carbon summary:\n"
            f"{_format_carbon(carbon_results)}\n\n"
            f"{_format_itinerary(ranked_results)}\n\n"
            "Ethics note: carbon figures are estimates for comparison and should not be presented as proof of zero-impact or fully sustainable travel.\n\n"
            "Recent conversation:\n"
            f"{_summarise_events(tracker)}"
        )


class ActionDefaultFallback(Action):
    def name(self) -> Text:
        return "action_default_fallback"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[EventType]:
        count = int(_safe_float(tracker.get_slot("fallback_count"), 0)) + 1
        if count == 1:
            dispatcher.utter_message(response="utter_ask_clarification")
            return [SlotSet("fallback_count", count)]
        if count == 2:
            dispatcher.utter_message(
                text="I can route this back into trip planning, recommendations, carbon estimates, or human support.",
                buttons=[
                    {"title": "Trip planning", "payload": "/book_trip"},
                    {"title": "Carbon estimate", "payload": "/ask_carbon"},
                    {"title": "Human support", "payload": "/ask_handover"},
                ],
            )
            return [SlotSet("fallback_count", count)]
        dispatcher.utter_message(text="I am escalating this so a human can help without you repeating everything.")
        return [SlotSet("fallback_count", count), FollowupAction("action_handover_to_human")]


class ActionResetTrip(Action):
    def name(self) -> Text:
        return "action_reset_trip"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[EventType]:
        dispatcher.utter_message(text="I have cleared the current trip context.")
        return [
            SlotSet("destination", None),
            SlotSet("start_date", None),
            SlotSet("end_date", None),
            SlotSet("budget", None),
            SlotSet("sustainability_preference", None),
            SlotSet("carbon_results", None),
            SlotSet("travel_options", None),
            SlotSet("ranked_results", None),
            SlotSet("fallback_count", 0),
        ]
