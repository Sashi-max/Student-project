const rasaUrl = window.ECO_TRAVEL_REST_URL || "http://localhost:5005/webhooks/rest/webhook";
const sender = `eco-web-${Math.random().toString(36).slice(2)}`;

const messages = document.querySelector("#messages");
const form = document.querySelector("#chatForm");
const input = document.querySelector("#messageInput");
const statusDot = document.querySelector("#statusDot");

function addMessage(text, role = "bot") {
  const bubble = document.createElement("div");
  bubble.className = `message ${role}`;
  bubble.textContent = text;
  messages.appendChild(bubble);
  messages.scrollTop = messages.scrollHeight;
}

function addButtons(buttons) {
  if (!buttons || !buttons.length) return;
  const row = document.createElement("div");
  row.className = "button-row";
  buttons.forEach((button) => {
    const element = document.createElement("button");
    element.type = "button";
    element.textContent = button.title;
    element.addEventListener("click", () => sendMessage(button.payload || button.title));
    row.appendChild(element);
  });
  messages.appendChild(row);
  messages.scrollTop = messages.scrollHeight;
}

function addStructuredPayload(payload) {
  if (!payload || typeof payload !== "object") return;

  if (payload.type === "hotel_card" && payload.hotel) {
    const card = document.createElement("article");
    card.className = "result-card";
    card.innerHTML = `
      <div>
        <h3>${payload.hotel.name || "Hotel option"}</h3>
        <p>${payload.hotel.currency || "GBP"} ${payload.hotel.price || "-"} per night</p>
      </div>
      <span class="carbon-pill ${payload.hotel.carbon_label || "amber"}">${payload.hotel.carbon_label || "amber"}</span>
    `;
    messages.appendChild(card);
  }

  if (payload.type === "carbon_summary" && payload.results) {
    const card = document.createElement("article");
    card.className = "result-card stacked";
    const rows = Object.entries(payload.results)
      .map(([mode, item]) => `<p><strong>${mode}</strong>: ${item.kg_co2e} kg CO2e <span class="carbon-pill ${item.label}">${item.label}</span></p>`)
      .join("");
    card.innerHTML = `<h3>Carbon estimate</h3>${rows}`;
    messages.appendChild(card);
  }
}

async function sendMessage(text) {
  const clean = String(text || "").trim();
  if (!clean) return;

  addMessage(clean, "user");
  input.value = "";
  statusDot.textContent = "Sending";

  try {
    const response = await fetch(rasaUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sender, message: clean }),
    });

    if (!response.ok) {
      throw new Error(`Rasa returned ${response.status}`);
    }

    const botMessages = await response.json();
    if (!botMessages.length) {
      addMessage("I did not receive a reply. Try rephrasing that.");
    }
    botMessages.forEach((message) => {
      if (message.text) addMessage(message.text, "bot");
      addButtons(message.buttons);
      addStructuredPayload(message.custom || message.json_message);
    });
    statusDot.textContent = "REST";
  } catch (error) {
    statusDot.textContent = "Offline";
    addMessage("I cannot reach Rasa yet. Check that the Rasa server is running on port 5005.", "bot");
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  sendMessage(input.value);
});

document.querySelectorAll("[data-payload]").forEach((button) => {
  button.addEventListener("click", () => sendMessage(button.dataset.payload));
});

sendMessage("/greet");
