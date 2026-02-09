module.exports = {
  apps: [
    {
      name: "basebot",
      script: "/root/basebot/venv/bin/python3",  // venv Python (has all deps)
      args: "main.py",
      cwd: "/root/basebot",           // ← change to your VPS path
      interpreter: "none",             // script IS the interpreter
      autorestart: true,
      watch: false,
      max_restarts: 20,
      restart_delay: 30000,            // 30s between restarts (avoid rate limit loops)
      max_memory_restart: "500M",
      env: {
        PYTHONUNBUFFERED: "1",         // force unbuffered output for real-time logs
      },
      // Log configuration
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      error_file: "/root/basebot/logs/error.log",
      out_file: "/root/basebot/logs/out.log",
      merge_logs: true,
      log_type: "json",
    },
  ],
};
