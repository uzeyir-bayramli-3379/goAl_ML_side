from google import genai

import os
from dotenv import load_dotenv
load_dotenv()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
response=client.interactions.create(
    model='gemini-2.5-flash',
    system_instruction='You are a landmark guide , travel helper for people who have come to a foreign country with little to no information. ' \
    'Your goal is to give basic information about landmarks to help these foreigners get to know about the city they are in. Do not oversell anything , your main objective is to give small',
    input='Generate short overview for Maiden Tower'
)
print(response.output_text)