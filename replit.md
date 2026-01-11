# Telegram AI Bot

## Overview
A Telegram bot powered by GPT-3.5 that provides AI chat and image generation capabilities. Features subscription-based access with multiple payment options.

## Project Structure
- `bot.py` - Main bot application
- `requirements.txt` - Python dependencies
- `user_contexts.db` - SQLite database for user data (auto-created)

## Features
- GPT-3.5 text generation
- Image generation via DALL-E
- First 10 messages free, then subscription required
- Payment options: Telegram Payments, Qiwi, Card (Mir)
- User context/history persistence

## Required Environment Variables
The bot requires the following secrets to be configured:
- `TELEGRAM_TOKEN` - Telegram Bot API token from @BotFather
- `OPENAI_API_KEY` - OpenAI API key for GPT and image generation
- `PAYMENT_PROVIDER_TOKEN` - (Optional) Telegram Payments provider token
- `QIWI_API_KEY` - (Optional) Qiwi API token
- `QIWI_PHONE` - (Optional) Qiwi wallet phone number
- `CARD_MIR_NUMBER` - (Optional) Mir card number for payments
- `CARD_MIR_AMOUNT` - (Optional) Payment amount, defaults to 30

## Running the Bot
The bot runs via `python bot.py` and uses polling mode.

## Tech Stack
- Python 3.11
- python-telegram-bot 20.3
- OpenAI API (v1.0+ client)
- SQLite for data persistence
