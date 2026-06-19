import subprocess
import time

print("Starting Rasa...")

rasa_process = subprocess.Popen([
    "rasa", "run",
    "--enable-api",
    "--port", "5005",
    "--cors", "*"
])

print("Starting Action Server...")

action_process = subprocess.Popen([
    "rasa", "run", "actions",
    "--port", "5055"
])

while True:
    time.sleep(5)