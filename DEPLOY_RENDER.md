Deploy FigPoint to Render (one shared URL)

What I already prepared in this repo:
- Dockerfile
- .dockerignore
- render.yaml

Step 1: Put this project in GitHub
1. Create a new GitHub repository (private or internal is fine).
2. From the project folder, run:
   git init
   git add .
   git commit -m "Initial FigPoint deployment setup"
   git branch -M main
   git remote add origin <your-github-repo-url>
   git push -u origin main

Step 2: Deploy on Render
1. In Render, choose New + and then Blueprint.
2. Select your GitHub repo.
3. Render will detect render.yaml automatically.
4. Click Apply.

Step 3: Set environment variables in Render service settings
Required now:
- WEBHOOK_BASE_URL = https://<your-render-service>.onrender.com

Optional (only when you re-enable Microsoft sync card):
- MS_CLIENT_ID
- MS_TENANT
- MS_REDIRECT_URI
- MS_LOGIN_HINT

Notes:
- DECKS_OUTPUT_DIR is already set in render.yaml to /data/decks.
- Persistent disk is already configured in render.yaml at /data.

Step 4: Verify deployment
1. Open your Render URL.
2. Confirm homepage loads.
3. Create a test deck from a Figma URL.
4. Confirm deck appears and build progress updates.

Step 5: Share with colleagues
- Send only the Render URL.
- No zip, no local install required.

Operational notes
- If your Render app URL changes, update WEBHOOK_BASE_URL to match.
- For Figma live updates via webhooks, the URL must be HTTPS and publicly reachable.
