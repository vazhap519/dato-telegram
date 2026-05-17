#!/bin/bash

echo "🚀 Starting project..."

# 1. Create venv
if [ ! -d "venv" ]; then
    echo "📦 Creating venv..."
    python -m venv venv
fi

# 2. Activate venv (Windows fix)
echo "⚡ Activating venv..."
source venv/Scripts/activate

# 3. Fix PYTHONPATH (VERY IMPORTANT)
export PYTHONPATH=$(pwd)

# 4. Upgrade pip
echo "⬆️ Upgrading pip..."
python -m pip install --upgrade pip

# 5. Install requirements
echo "📥 Installing requirements..."
python -m pip install -r requirements.txt

# 6. Init DB
echo "🗄️ Initializing DB..."
python db/init_db.py

# 7. Start Admin Panel
echo "🌐 Starting Admin Panel..."
python -m uvicorn admin.main:app --reload &

# 8. Wait ცოტა
sleep 2

# 9. Start Bot
echo "🤖 Starting Bot..."
python -m bot.main

echo "🛑 Stopped"