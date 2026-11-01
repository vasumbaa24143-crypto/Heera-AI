"""One Sarvam chat completion call. Reads SARVAM_API_KEY from the environment."""
import os
import sys

from sarvamai import SarvamAI

key = os.environ.get("SARVAM_API_KEY")
if not key:
    sys.exit("SARVAM_API_KEY is not set. Put it in .env and load it into the environment.")

client = SarvamAI(api_subscription_key=key)
response = client.chat.completions(
    model="sarvam-105b-conversations",
    messages=[{"role": "user", "content": "Say hello in one short sentence."}],
)
print(response.choices[0].message.content)
