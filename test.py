import os
import dotenv
from openai import OpenAI

dotenv.load_dotenv()

client = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=os.getenv("GEMMA_API_KEY")
)

#landmarks = [
#    {"name": "Maiden Tower", "type": "historic tower", "built": "12th century"},
#    {"name": "Flame Towers", "type": "skyscraper", "built": "2012"},
#    {"name": "Fake Landmark XYZ", "type": "unknown", "built": "unknown"},  # edge case
#]
#for i in range (10):
#    lm=landmarks[-1]
#    system_prompt = "You are a landmark guide. Use ONLY the facts given. If unsure, dont dress up the absence of information , instead say something like 'no information found on X Y Z ....' "
#    user_prompt = f"Landmark: {lm['name']}\nType: {lm['type']}\nBuilt: {lm['built']}\nGenerate a short overview."
#    response = client.chat.completions.create(
#        model="google/gemma-2-2b-it",
#        messages=[
#            {"role": "assistant", "content": system_prompt},
#            {"role": "user", "content": user_prompt}
#        ]
#    )
#    print(f"--- {lm['name']} ---")
#    print(response.choices[0].message.content)
#    print()



system_prompt = "You are a landmark , travel guide. If unsure of certain details , dont dress up the absence ,do not make up stuff you are not sure of, instead say something like 'no information found on X Y Z ....' , do not write long paragraphs , just short general information"
user_prompt = f"Landmark: the Maiden Tower in Baku\nGenerate a short overview 1-2 short sentences. Write Name , date of creation , type of tower and short historic background if sure of the information"
response = client.chat.completions.create(
    model="google/gemma-3n-e2b-it",
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]
)
print(response.choices[0].message.content)
print()