# Telegram AI Bot

## Overview
A Telegram bot powered by GPT-4o that provides AI chat with text and photo analysis capabilities. Features subscription-based access with YooKassa payment processing.

## Project Structure
- `bot.py` - Main bot application
- `requirements.txt` - Python dependencies
- `user_contexts.db` - SQLite database for user data (auto-created)

## Features
- GPT-4o text generation
- Photo analysis with GPT-4o Vision (send photo to get analysis/solve tasks)
- First 10 messages free, then subscription required (30₽/month)
- Payment via YooKassa (bank cards)
- Admin commands for subscription management
- User context/history persistence

## Admin Commands
- `/activate_sub <user_id> [months]` - Activate subscription for a user
- `/admin_stats` - View bot statistics
- `/admin_broadcast <message>` - Send message to all users

## Required Environment Variables
- `TELEGRAM_TOKEN` - Telegram Bot API token from @BotFather
- `OPENAI_API_KEY` - OpenAI API key for GPT-4o
- `YOOKASSA_SHOP_ID` - YooKassa shop ID
- `YOOKASSA_SECRET_KEY` - YooKassa secret key

## Deployment
- Development: Only health check server runs (no bot)
- Production: Full bot runs via `python bot.py`
- This prevents duplicate messages from multiple bot instances

## Tech Stack
- Python 3.11
- python-telegram-bot 20.3
- OpenAI API (GPT-4o with Vision)
- YooKassa payment SDK
- SQLite for data persistence
