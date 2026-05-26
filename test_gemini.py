from google import genai
import json

with open("config.json") as f:
    key = json.load(f).get("gemini_api_key", "")

client = genai.Client(api_key=key, http_options={"api_version": "v1beta"})

test_msg = """Name : <@1233480562455609385>
Total Hours   5 hurs 53 mutes
Date : 05/25/2026"""

prompt = (
    "You are parsing an EMS attendance log message. "
    "Extract ONLY the total working hours and minutes from the 'Total Hours' field. "
    "The field may be misspelled. "
    "Respond with ONLY two integers on one line separated by a space: HOURS MINUTES. "
    "No other text.\n\n"
    f"Attendance message:\n{test_msg}"
)

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents=prompt
)
print("Raw response:", repr(response.text.strip()))
parts = response.text.strip().split()
print(f"Parsed → {parts[0]}h {parts[1]}m")