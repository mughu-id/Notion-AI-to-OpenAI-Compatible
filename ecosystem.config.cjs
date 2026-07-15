/**
 * PM2 process file for NotionChat.
 *
 * Usage (from project root after setup):
 *   npm i -g pm2
 *   pm2 start ecosystem.config.cjs
 *   pm2 status
 *   pm2 logs notionchat
 *   pm2 restart notionchat
 *   pm2 stop notionchat
 *   pm2 save && pm2 startup   # optional: start on boot
 *
 * Requires: Python venv at .venv/ with `pip install -r requirements.txt && pip install -e .`
 * Configure via .env (copy from .env.example). Do not put secrets in this file.
 */
const path = require("path");
const fs = require("fs");

const root = __dirname;
const isWin = process.platform === "win32";
const venvPython = isWin
  ? path.join(root, ".venv", "Scripts", "python.exe")
  : path.join(root, ".venv", "bin", "python");
const systemPython = isWin ? "python" : "python3";
const python = fs.existsSync(venvPython) ? venvPython : systemPython;

module.exports = {
  apps: [
    {
      name: "notionchat",
      cwd: root,
      script: python,
      args: "-m notionchat serve",
      interpreter: "none",
      instances: 1,
      exec_mode: "fork",
      autorestart: true,
      watch: false,
      max_memory_restart: "512M",
      min_uptime: "5s",
      max_restarts: 20,
      restart_delay: 3000,
      kill_timeout: 8000,
      env: {
        NOTIONCHAT_HOME: root,
        PYTHONUNBUFFERED: "1",
        PYTHONIOENCODING: "utf-8",
      },
      error_file: path.join(root, "logs", "pm2-error.log"),
      out_file: path.join(root, "logs", "pm2-out.log"),
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",
      merge_logs: true,
      time: true,
    },
  ],
};
