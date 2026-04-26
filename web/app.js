(function () {
  'use strict';

  const healthDot = document.getElementById('health-dot');
  const healthText = document.getElementById('health-text');
  const errorPanel = document.getElementById('error-panel');
  const form = document.getElementById('command-form');
  const input = document.getElementById('command-input');
  const sendButton = document.getElementById('send-button');
  const transcript = document.getElementById('transcript');
  const transcriptCount = document.getElementById('transcript-count');
  const clearButton = document.getElementById('clear-button');
  const commandsList = document.getElementById('commands-list');
  const csrfToken = document.querySelector('meta[name="vcpi-csrf-token"]')?.content || '';

  let entryCount = 0;
  let commandInFlight = false;

  function setCommandControlsDisabled(disabled) {
    commandInFlight = disabled;
    sendButton.disabled = disabled;
    document.querySelectorAll('[data-command]').forEach((button) => {
      button.disabled = disabled;
    });
  }

  function setHealth(state, message) {
    healthDot.className = `health-dot health-dot--${state}`;
    healthText.textContent = message;
  }

  function showError(message) {
    if (!message) {
      errorPanel.hidden = true;
      errorPanel.textContent = '';
      return;
    }
    errorPanel.textContent = message;
    errorPanel.hidden = false;
  }

  function normalizeOutput(value) {
    if (Array.isArray(value)) {
      return value.join('\n');
    }
    if (typeof value === 'string') {
      return value;
    }
    if (value == null) {
      return '';
    }
    return String(value);
  }

  async function readJson(response) {
    const text = await response.text();
    if (!text) {
      return {};
    }
    try {
      return JSON.parse(text);
    } catch (error) {
      throw new Error(`Expected JSON but received: ${text.slice(0, 160)}`);
    }
  }

  function appendTranscript(command, payload) {
    if (entryCount === 0) {
      transcript.textContent = '';
    }

    const entry = document.createElement('article');
    entry.className = payload.ok ? 'entry entry--ok' : 'entry entry--error';

    const meta = document.createElement('div');
    meta.className = 'entry-meta';
    const prompt = document.createElement('span');
    prompt.textContent = `vcpi> ${command}`;
    const status = document.createElement('span');
    status.textContent = payload.ok ? 'ok' : 'error';
    meta.append(prompt, status);

    const pre = document.createElement('pre');
    pre.textContent = payload.ok
      ? normalizeOutput(payload.output) || '(no output)'
      : normalizeOutput(payload.error) || 'Command failed';

    entry.append(meta, pre);
    transcript.append(entry);
    entryCount += 1;
    transcriptCount.textContent = `${entryCount} ${entryCount === 1 ? 'entry' : 'entries'}`;
    transcript.scrollTop = transcript.scrollHeight;
  }

  async function checkHealth() {
    setHealth('checking', 'Checking connection…');
    try {
      const response = await fetch('/api/health', {headers: {'Accept': 'application/json'}});
      const data = await readJson(response);
      if (response.ok && data.ok) {
        setHealth('ok', `Connected${data.socket ? ` via ${data.socket}` : ''}`);
        return;
      }
      setHealth('bad', data.error || `Health check failed (${response.status})`);
    } catch (error) {
      setHealth('bad', error.message || String(error));
    }
  }

  async function loadCommands() {
    try {
      const response = await fetch('/api/commands', {headers: {'Accept': 'application/json'}});
      const data = await readJson(response);
      const commands = Array.isArray(data.commands) ? data.commands : [];
      if (commands.length === 0) {
        commandsList.textContent = data.error || 'No command names returned.';
        return;
      }
      commandsList.textContent = '';
      commands.forEach((command) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'command-chip';
        button.textContent = command;
        button.addEventListener('click', () => {
          input.value = command;
          input.focus();
        });
        commandsList.append(button);
      });
      if (!response.ok || data.ok === false) {
        showError(data.error || 'Using fallback command list.');
      }
    } catch (error) {
      commandsList.textContent = 'Command names are unavailable.';
      showError(error.message || String(error));
    }
  }

  async function runCommand(command) {
    if (commandInFlight) {
      showError('Wait for the current command to finish before sending another one.');
      return;
    }
    showError('');
    setCommandControlsDisabled(true);
    sendButton.textContent = 'Sending…';
    try {
      const response = await fetch('/api/command', {
        method: 'POST',
        headers: {
          'Accept': 'application/json',
          'Content-Type': 'application/json',
          'X-VCPI-CSRF': csrfToken,
        },
        body: JSON.stringify({command}),
      });
      const data = await readJson(response);
      const payload = response.ok ? data : {...data, ok: false};
      appendTranscript(command, payload);
      if (!payload.ok) {
        showError(payload.error || `Command failed (${response.status})`);
      }
      checkHealth();
    } catch (error) {
      const message = error.message || String(error);
      appendTranscript(command, {ok: false, error: message});
      showError(message);
    } finally {
      setCommandControlsDisabled(false);
      sendButton.textContent = 'Send';
      input.focus();
    }
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const command = input.value.trim();
    if (!command) {
      showError('Enter a command before sending.');
      input.focus();
      return;
    }
    input.value = '';
    runCommand(command);
  });

  document.querySelectorAll('[data-command]').forEach((button) => {
    button.addEventListener('click', () => {
      const command = button.getAttribute('data-command');
      if (command) {
        input.value = command;
        runCommand(command);
      }
    });
  });

  clearButton.addEventListener('click', () => {
    entryCount = 0;
    transcript.innerHTML = '<p class="empty-state">No commands sent yet.</p>';
    transcriptCount.textContent = '0 entries';
    showError('');
    input.focus();
  });

  checkHealth();
  loadCommands();
})();
