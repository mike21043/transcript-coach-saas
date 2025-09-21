#!/usr/bin/env bash
# Interactive helper to create a .env file at the repo root with required secrets
# Usage: bash scripts/setup_env.sh

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"


echo "This script will create $ENV_FILE with VAST and GitHub credentials."
echo "It will set file permissions to 600 so it's readable only by you."

if [ -f "$ENV_FILE" ]; then
	echo "WARNING: $ENV_FILE already exists."
	echo "  (O)verwrite  (B)ackup and overwrite  (A)bort"
	read -p "Choose an action [A]: " ACTION
	ACTION=${ACTION:-A}
	case "${ACTION^^}" in
		O)
			echo "Overwriting $ENV_FILE..." ;;
		B)
			BACKUP="$ENV_FILE.bak.$(date +%s)"
			cp "$ENV_FILE" "$BACKUP"
			chmod 600 "$BACKUP"
			echo "Backed up existing .env to $BACKUP" ;;
		*)
			echo "Aborting. Existing .env preserved." ; exit 1 ;;
	esac
fi

read -p "Vast API key (VAST_API_KEY): " VAST_API_KEY
read -p "Vast template hash (VAST_TEMPLATE_HASH): " VAST_TEMPLATE_HASH
read -p "GitHub PAT (GITHUB_PAT) [will be stored locally, do not share]: " GITHUB_PAT
read -p "GitHub owner (GITHUB_OWNER) [default: mike21043]: " GITHUB_OWNER
read -p "Image repo (IMAGE_REPO) [default: transcript-coach-saas]: " IMAGE_REPO

GITHUB_OWNER=${GITHUB_OWNER:-mike21043}
IMAGE_REPO=${IMAGE_REPO:-transcript-coach-saas}

cat > "$ENV_FILE" <<EOF
VAST_API_KEY="$VAST_API_KEY"
VAST_TEMPLATE_HASH="$VAST_TEMPLATE_HASH"
GITHUB_PAT="$GITHUB_PAT"
GITHUB_OWNER="$GITHUB_OWNER"
IMAGE_REPO="$IMAGE_REPO"
EOF

chmod 600 "$ENV_FILE"
echo "Wrote $ENV_FILE (permissions 600)."
echo "You can now run: source scripts/export_env.sh  # to load into your shell" 
echo "Or run the harness directly: python3 scripts/e2e_provision_and_test.py"
