"""
services/bedrock_client.py
───────────────────────────
WHAT THIS FILE DOES:
  AWS Bedrock client — used ONLY by classify_intent node.
  Called once per session (Turn 1 only).

BEFORE (Strands):
  Bedrock called EVERY turn — full history sent each time
  LLM controlled tool execution

AFTER (LangGraph):
  Bedrock called ONCE — classify_intent node only
  Returns JSON: {intent, inputs}
  Graph takes over — no more Bedrock calls
"""
"""
services/bedrock_client.py
───────────────────────────
Supports:
  - Amazon Nova  (amazon.nova-*)
  - Claude 3.x   (anthropic.claude-3*)
  - Claude 2     (anthropic.claude-v2, claude-instant)

Reads from .env:
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, BEDROCK_MODEL_ID
"""
import json
import logging
import os
import boto3
from dotenv import load_dotenv

load_dotenv()
logger           = logging.getLogger(__name__)
AWS_REGION       = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-pro-v1:0")
_client = None


def get_bedrock_client():
    global _client
    if _client is None:
        ak = os.environ.get("AWS_ACCESS_KEY_ID", "")
        sk = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        if ak and sk and "YOUR" not in ak:
            _client = boto3.client(
                "bedrock-runtime",
                region_name=AWS_REGION,
                aws_access_key_id=ak,
                aws_secret_access_key=sk,
            )
        else:
            _client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    return _client


def invoke_claude(system_prompt, user_message, max_tokens=512, temperature=0.0):
    model  = BEDROCK_MODEL_ID
    client = get_bedrock_client()

    # ── Amazon Nova Pro / Lite / Micro ────────────────────────────────────
    if "nova" in model.lower():
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"text": system_prompt + "\n\n" + user_message}
                    ]
                }
            ],
            "inferenceConfig": {
                "max_new_tokens": max_tokens,
                "temperature":    temperature,
            },
        }
        try:
            resp   = client.invoke_model(
                modelId=model,
                body=json.dumps(body),
                contentType="application/json",
                accept="application/json",
            )
            result = json.loads(resp["body"].read())
            text   = (result
                      .get("output", {})
                      .get("message", {})
                      .get("content", [{}])[0]
                      .get("text", ""))
            if not text:
                text = str(result)
            logging.info("Nova OK | %r", text[:80])
            return text.strip()
        except Exception as exc:
            raise RuntimeError(f"Nova failed: {exc}") from exc

    # ── Claude 3.x / 3.5 ─────────────────────────────────────────────────
    if any(x in model for x in ("claude-3", "sonnet", "haiku", "opus")):
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens":  max_tokens,
            "temperature": temperature,
            "system":      system_prompt,
            "messages":    [{"role": "user", "content": user_message}],
        }
        try:
            resp   = client.invoke_model(
                modelId=model,
                body=json.dumps(body),
                contentType="application/json",
                accept="application/json",
            )
            result = json.loads(resp["body"].read())
            if "content" in result:
                return result["content"][0]["text"].strip()
            return result.get("completion", "").strip()
        except Exception as exc:
            raise RuntimeError(f"Claude 3 failed: {exc}") from exc

    # ── Claude 2 / Instant ────────────────────────────────────────────────
    body = {
        "prompt": "\n\nHuman: " + system_prompt + "\n\n" + user_message + "\n\nAssistant:",
        "max_tokens_to_sample": max_tokens,
        "temperature": temperature,
    }
    try:
        resp   = client.invoke_model(
            modelId=model,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
        result = json.loads(resp["body"].read())
        return result.get("completion", "").strip()
    except Exception as exc:
        raise RuntimeError(f"Claude 2 failed: {exc}") from exc
