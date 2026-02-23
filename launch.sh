#!/bin/zsh
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

echo "========================================="
echo "          卒業bot 起動スクリプト"
echo "========================================="
echo ""

# venv がなければ作成
if [ ! -d ".venv" ]; then
    echo "▶ 仮想環境を作成中..."
    python3 -m venv .venv
    echo "✓ 仮想環境を作成しました"
fi

# 仮想環境を有効化
source .venv/bin/activate

# 依存パッケージのインストール（差分のみ）
echo "▶ 依存パッケージを確認中..."
pip install -q -r requirements.txt
echo "✓ パッケージ準備完了"

# .env チェック
if [ ! -f ".env" ]; then
    echo ""
    echo "⚠️  .env ファイルが見つかりません"
    echo "   TELEGRAM_BOT_TOKEN と TELEGRAM_CHAT_ID を .env に設定してください"
    echo ""
    read "?Enterキーで閉じます..."
    exit 1
fi

echo ""
echo "▶ 卒業bot を起動します..."
echo "-----------------------------------------"
python main.py

echo ""
read "?終了しました。Enterキーで閉じます..."
