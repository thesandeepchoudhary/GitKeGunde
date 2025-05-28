import openai
import os

openai.api_key = os.getenv("OPENAI_API_KEY")

def generate_review(mcp_context):
    messages = [
        {"role": msg["role"], "content": msg["content"] if isinstance(msg["content"], str) else str(msg["content"])}
        for msg in mcp_context["messages"]
    ]
    
    response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=messages
    )
    return response.choices[0].message.content