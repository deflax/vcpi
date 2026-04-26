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
  const typedRefreshButton = document.getElementById('typed-refresh-button');
  const typedRefreshStatus = document.getElementById('typed-refresh-status');
  const audioStartButton = document.getElementById('audio-start-button');
  const audioStopButton = document.getElementById('audio-stop-button');
  const typedError = document.getElementById('typed-error');
  const masterGainInput = document.getElementById('master-gain-input');
  const masterGainValue = document.getElementById('master-gain-value');
  const sessionNameInput = document.getElementById('session-name-input');
  const sessionSaveButton = document.getElementById('session-save-button');
  const sessionLoadButton = document.getElementById('session-load-button');
  const sessionControlStatus = document.getElementById('session-control-status');
  const statusFields = {
    daemon: document.getElementById('status-daemon'),
    audio: document.getElementById('status-audio'),
    session: document.getElementById('status-session'),
    link: document.getElementById('status-link'),
    midi: document.getElementById('status-midi'),
  };
  const slotsList = document.getElementById('slots-list');
  const slotsCount = document.getElementById('slots-count');
  const csrfToken = document.querySelector('meta[name="vcpi-csrf-token"]')?.content || '';

  let entryCount = 0;
  let commandInFlight = false;
  let typedMutationInFlight = false;
  let typedRefreshInFlight = false;
  let typedRefreshPromise = null;
  let typedRefreshTimer = 0;
  let sessionNameDirty = false;

  const typedRefreshVisibleIntervalMs = 10000;
  const typedRefreshHiddenIntervalMs = 60000;

  function updateTypedControlsDisabled() {
    const disabled = typedMutationInFlight || typedRefreshInFlight;
    typedRefreshButton.disabled = disabled;
    audioStartButton.disabled = disabled;
    audioStopButton.disabled = disabled;
    document.querySelectorAll('[data-typed-action]').forEach((control) => {
      control.disabled = disabled;
    });
  }

  function setCommandControlsDisabled(disabled) {
    commandInFlight = disabled;
    sendButton.disabled = disabled;
    document.querySelectorAll('[data-command]').forEach((button) => {
      button.disabled = disabled;
    });
  }

  function setTypedControlsDisabled(disabled) {
    typedMutationInFlight = disabled;
    updateTypedControlsDisabled();
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

  function showTypedError(message) {
    if (!message) {
      typedError.hidden = true;
      typedError.textContent = '';
      return;
    }
    typedError.textContent = message;
    typedError.hidden = false;
  }

  function setTypedRefreshStatus(message) {
    typedRefreshStatus.textContent = message;
  }

  function setSessionControlStatus(message) {
    sessionControlStatus.textContent = message;
  }

  function typedRefreshIntervalLabel() {
    return document.hidden ? 'Auto-refresh slowed while hidden' : 'Auto-refresh every 10s';
  }

  function formatRefreshTime(date) {
    return date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  }

  function nextTypedRefreshDelay() {
    return document.hidden ? typedRefreshHiddenIntervalMs : typedRefreshVisibleIntervalMs;
  }

  function scheduleTypedRefresh(delay) {
    window.clearTimeout(typedRefreshTimer);
    typedRefreshTimer = window.setTimeout(runTypedRefreshPoll, delay);
  }

  function scheduleNextTypedRefresh() {
    scheduleTypedRefresh(nextTypedRefreshDelay());
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

  function formatValue(value, fallback) {
    if (Array.isArray(value)) {
      return value.length > 0 ? value.join(', ') : fallback;
    }
    if (typeof value === 'boolean') {
      return value ? 'On' : 'Off';
    }
    if (typeof value === 'number' && Number.isFinite(value)) {
      return Number.isInteger(value) ? String(value) : value.toFixed(2);
    }
    if (typeof value === 'string' && value.trim()) {
      return value;
    }
    return fallback;
  }

  function firstPresent(source, keys, fallback) {
    if (!source || typeof source !== 'object') {
      return fallback;
    }
    for (const key of keys) {
      if (Object.prototype.hasOwnProperty.call(source, key) && source[key] != null) {
        return source[key];
      }
    }
    return fallback;
  }

  function asObject(value) {
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
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

  async function fetchJson(path, options) {
    const response = await fetch(path, options);
    const data = await readJson(response);
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || `${path} failed (${response.status})`);
    }
    return data;
  }

  async function postTyped(path, payload) {
    return fetchJson(path, {
      method: 'POST',
      headers: {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'X-VCPI-CSRF': csrfToken,
      },
      body: JSON.stringify(payload || {}),
    });
  }

  function makeEmptyState(message) {
    const empty = document.createElement('p');
    empty.className = 'empty-state';
    empty.textContent = message;
    return empty;
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

  function statusText(value, detail) {
    const base = formatValue(value, 'Unknown');
    return detail ? `${base} · ${detail}` : base;
  }

  function normalizeGain(value, fallback) {
    const number = Number(value);
    if (!Number.isFinite(number)) {
      return fallback;
    }
    return Math.min(1, Math.max(0, number));
  }

  function renderMasterGain(audio) {
    const gain = firstPresent(audio, ['master_gain'], undefined);
    if (gain == null) {
      masterGainValue.textContent = 'Not reported by typed status';
      masterGainInput.value = '1';
      return;
    }
    const normalizedGain = normalizeGain(gain, 1);
    masterGainValue.textContent = formatValue(normalizedGain, 'Unknown');
    masterGainInput.value = normalizedGain.toFixed(2);
  }

  function currentSessionName(session, status) {
    const value = firstPresent(
      session,
      ['name', 'loaded_name', 'current_name', 'session_name', 'loaded'],
      firstPresent(status, ['session_name', 'current_session', 'loaded_session'], ''),
    );
    return typeof value === 'string' && value.trim() ? value.trim() : '';
  }

  function syncSessionNameInput(session, status) {
    const name = currentSessionName(session, status);
    if (!name) {
      return;
    }
    if (document.activeElement === sessionNameInput || sessionNameDirty) {
      return;
    }
    sessionNameInput.value = name;
  }

  function renderStatus(data) {
    const status = asObject(data.status || data);
    const daemon = asObject(status.daemon);
    const audio = asObject(status.audio);
    const session = asObject(status.session);
    const link = asObject(status.link || status.ableton_link);
    const midi = asObject(status.midi);

    statusFields.daemon.textContent = statusText(
      firstPresent(daemon, ['state', 'status', 'running', 'ok'], firstPresent(status, ['daemon_state', 'daemon'], data.ok)),
      formatValue(firstPresent(daemon, ['socket', 'pid', 'version'], ''), ''),
    );
    statusFields.audio.textContent = statusText(
      firstPresent(audio, ['state', 'status', 'running', 'active', 'started'], firstPresent(status, ['audio_running'], undefined)),
      formatValue(firstPresent(audio, ['device', 'sample_rate', 'samplerate'], ''), ''),
    );
    statusFields.session.textContent = statusText(
      firstPresent(session, ['name', 'path', 'loaded', 'dirty'], firstPresent(status, ['session'], undefined)),
      formatValue(firstPresent(session, ['modified', 'slot_count', 'slots'], ''), ''),
    );
    syncSessionNameInput(session, status);
    statusFields.link.textContent = statusText(
      firstPresent(link, ['enabled', 'active', 'running', 'state'], firstPresent(status, ['link_enabled'], undefined)),
      formatValue(firstPresent(link, ['tempo', 'bpm', 'peers'], ''), ''),
    );
    statusFields.midi.textContent = statusText(
      firstPresent(midi, ['active', 'enabled', 'input_count', 'inputs'], firstPresent(status, ['midi'], undefined)),
      formatValue(firstPresent(midi, ['ports', 'output_count', 'outputs'], ''), ''),
    );
    renderMasterGain(audio);
  }

  function normalizeSlots(data) {
    if (data && typeof data === 'object' && !Array.isArray(data) && !data.slots && !data.items && Object.prototype.hasOwnProperty.call(data, 'ok')) {
      return [];
    }
    const source = data.slots || data.items || data;
    if (Array.isArray(source)) {
      return source;
    }
    if (source && typeof source === 'object') {
      return Object.keys(source).map((key) => ({slot: key, ...asObject(source[key])}));
    }
    return [];
  }

  function appendSlotMeta(card, label, value) {
    const item = document.createElement('span');
    item.className = 'slot-meta-item';
    const labelNode = document.createElement('span');
    labelNode.textContent = label;
    const valueNode = document.createElement('strong');
    valueNode.textContent = formatValue(value, '—');
    item.append(labelNode, valueNode);
    card.append(item);
  }

  function typedActionButton(label, pressed, onClick, extraClassName) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = pressed ? 'typed-button typed-button--active' : 'typed-button typed-button--ghost';
    if (extraClassName) {
      button.className = `${button.className} ${extraClassName}`;
    }
    button.textContent = label;
    button.setAttribute('aria-pressed', pressed ? 'true' : 'false');
    button.dataset.typedAction = 'true';
    button.addEventListener('click', onClick);
    return button;
  }

  function renderSlots(data) {
    const slots = normalizeSlots(data);
    slotsList.textContent = '';
    slotsCount.textContent = `${slots.length} ${slots.length === 1 ? 'slot' : 'slots'}`;

    if (slots.length === 0) {
      slotsList.append(makeEmptyState('No slots returned by the typed API.'));
      return;
    }

    slots.forEach((rawSlot, index) => {
      const slot = asObject(rawSlot);
      const slotNumber = firstPresent(slot, ['slot', 'index', 'number', 'id'], index + 1);
      const gain = firstPresent(slot, ['gain', 'volume', 'level'], 1);
      const muted = Boolean(firstPresent(slot, ['muted', 'mute'], false));
      const soloed = Boolean(firstPresent(slot, ['soloed', 'solo'], false));
      const loaded = slot.loaded !== false;
      const slotName = formatValue(firstPresent(slot, ['name', 'plugin', 'instrument', 'type'], 'Loaded'), 'Loaded');

      const card = document.createElement('article');
      card.className = 'slot-card';

      const header = document.createElement('div');
      header.className = 'slot-card-header';
      const title = document.createElement('h3');
      title.textContent = `Slot ${slotNumber}`;
      const badge = document.createElement('span');
      badge.className = loaded ? 'slot-badge slot-badge--loaded' : 'slot-badge';
      badge.textContent = loaded ? slotName : 'Empty';
      header.append(title, badge);

      const meta = document.createElement('div');
      meta.className = 'slot-meta';
      appendSlotMeta(meta, 'Gain', gain);
      appendSlotMeta(meta, 'Mute', muted);
      appendSlotMeta(meta, 'Solo', soloed);
      appendSlotMeta(meta, 'MIDI', firstPresent(slot, ['midi_channels', 'midi', 'channel', 'midi_channel'], '—'));

      const controls = document.createElement('div');
      controls.className = 'slot-controls';

      const gainLabel = document.createElement('label');
      gainLabel.className = 'gain-control';
      const gainText = document.createElement('span');
      gainText.textContent = 'Gain';
      const gainInput = document.createElement('input');
      gainInput.type = 'range';
      gainInput.min = '0';
      gainInput.max = '1';
      gainInput.step = '0.01';
      gainInput.value = String(Number.isFinite(Number(gain)) ? Number(gain) : 1);
      gainInput.dataset.typedAction = 'true';
      gainInput.setAttribute('aria-label', `Set gain for slot ${slotNumber}`);
      gainInput.addEventListener('change', () => {
        mutateTyped(`/api/slots/${encodeURIComponent(slotNumber)}/gain`, {gain: Number(gainInput.value)});
      });
      gainLabel.append(gainText, gainInput);

      controls.append(
        gainLabel,
        typedActionButton(muted ? 'Unmute' : 'Mute', muted, () => {
          mutateTyped(`/api/slots/${encodeURIComponent(slotNumber)}/mute`, {muted: !muted});
        }),
        typedActionButton(soloed ? 'Unsolo' : 'Solo', soloed, () => {
          mutateTyped(`/api/slots/${encodeURIComponent(slotNumber)}/solo`, {solo: !soloed});
        }),
      );

      if (loaded) {
        controls.append(
          typedActionButton('Unload', false, () => {
            if (!window.confirm(`Unload slot ${slotNumber} (${slotName})?`)) {
              return;
            }
            mutateTyped(`/api/slots/${encodeURIComponent(slotNumber)}/clear`, {});
          }, 'typed-button--danger'),
        );
      }

      card.append(header, meta, controls);
      slotsList.append(card);
    });
  }

  async function refreshTypedApi(options) {
    const source = options && options.source ? options.source : 'manual';
    if (typedRefreshInFlight) {
      return typedRefreshPromise;
    }
    if (source === 'auto' && typedMutationInFlight) {
      setTypedRefreshStatus('Auto-refresh waiting for the current control update.');
      return Promise.resolve();
    }

    typedRefreshInFlight = true;
    updateTypedControlsDisabled();
    showTypedError('');
    setTypedRefreshStatus(source === 'auto' ? 'Auto-refreshing status and slots…' : 'Refreshing status and slots…');
    if (source !== 'auto') {
      typedRefreshButton.textContent = 'Refreshing…';
    }

    typedRefreshPromise = (async () => {
      const results = await Promise.allSettled([
        fetchJson('/api/status', {headers: {'Accept': 'application/json'}}),
        fetchJson('/api/slots', {headers: {'Accept': 'application/json'}}),
      ]);

      const messages = [];
      if (results[0].status === 'fulfilled') {
        renderStatus(results[0].value);
      } else {
        Object.values(statusFields).forEach((field) => {
          field.textContent = 'Typed endpoint unavailable';
        });
        masterGainValue.textContent = 'Typed endpoint unavailable';
        messages.push(results[0].reason.message || String(results[0].reason));
      }

      if (results[1].status === 'fulfilled') {
        renderSlots(results[1].value);
      } else {
        slotsList.textContent = '';
        slotsList.append(makeEmptyState('Typed slot endpoint is unavailable. The command console still works below.'));
        slotsCount.textContent = 'No typed slots';
        messages.push(results[1].reason.message || String(results[1].reason));
      }

      if (messages.length > 0) {
        showTypedError(`Typed API is not ready: ${messages.join(' · ')}`);
        setTypedRefreshStatus(`Last refresh failed at ${formatRefreshTime(new Date())} · ${typedRefreshIntervalLabel()}`);
      } else {
        setTypedRefreshStatus(`Updated ${formatRefreshTime(new Date())} · ${typedRefreshIntervalLabel()}`);
      }

      return results;
    })();

    try {
      return await typedRefreshPromise;
    } catch (error) {
      showTypedError(error.message || String(error));
      setTypedRefreshStatus(`Last refresh failed at ${formatRefreshTime(new Date())} · ${typedRefreshIntervalLabel()}`);
      return undefined;
    } finally {
      typedRefreshInFlight = false;
      typedRefreshPromise = null;
      typedRefreshButton.textContent = 'Refresh';
      updateTypedControlsDisabled();
      if (source !== 'auto') {
        scheduleNextTypedRefresh();
      }
    }
  }

  async function runTypedRefreshPoll() {
    if (typedMutationInFlight) {
      setTypedRefreshStatus('Auto-refresh waiting for the current control update.');
      scheduleNextTypedRefresh();
      return;
    }
    await refreshTypedApi({source: 'auto'});
    scheduleNextTypedRefresh();
  }

  async function mutateTyped(path, payload) {
    if (typedMutationInFlight) {
      showTypedError('Wait for the current typed control update to finish.');
      return false;
    }
    showTypedError('');
    setTypedControlsDisabled(true);
    try {
      await postTyped(path, payload);
      await refreshTypedApi({source: 'mutation'});
      return true;
    } catch (error) {
      showTypedError(`Typed control unavailable: ${error.message || String(error)}`);
      return false;
    } finally {
      setTypedControlsDisabled(false);
    }
  }

  function sessionPayloadFromInput() {
    const name = sessionNameInput.value.trim();
    return name ? {name} : {};
  }

  function isSafeSessionName(name) {
    const normalized = name.toLowerCase().endsWith('.json') ? name.slice(0, -5) : name;
    return /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(normalized) && !normalized.includes('..');
  }

  function validateSessionName(name, required) {
    if (!name) {
      if (required) {
        return 'Enter a session name before loading.';
      }
      return '';
    }
    return isSafeSessionName(name) ? '' : 'Start with a letter or number and use only letters, numbers, dots, dashes, or underscores.';
  }

  async function saveSession() {
    const name = sessionNameInput.value.trim();
    const validationMessage = validateSessionName(name, false);
    if (validationMessage) {
      setSessionControlStatus(validationMessage);
      showTypedError(validationMessage);
      sessionNameInput.focus();
      return;
    }
    setSessionControlStatus('Saving session…');
    const ok = await mutateTyped('/api/session/save', sessionPayloadFromInput());
    if (ok) {
      sessionNameDirty = false;
      setSessionControlStatus('Session saved. Status and slots refreshed.');
    } else {
      setSessionControlStatus('Session save failed. See typed API status above.');
    }
  }

  async function loadSession() {
    const name = sessionNameInput.value.trim();
    const validationMessage = validateSessionName(name, true);
    if (validationMessage) {
      setSessionControlStatus(validationMessage);
      showTypedError(validationMessage);
      sessionNameInput.focus();
      return;
    }
    setSessionControlStatus(`Loading ${name}…`);
    const ok = await mutateTyped('/api/session/load', {name});
    if (ok) {
      sessionNameDirty = false;
      setSessionControlStatus('Session loaded. Status and slots refreshed.');
    } else {
      setSessionControlStatus('Session load failed. See typed API status above.');
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
      refreshTypedApi({source: 'command'});
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
    transcript.textContent = '';
    transcript.append(makeEmptyState('No commands sent yet.'));
    transcriptCount.textContent = '0 entries';
    showError('');
    input.focus();
  });

  typedRefreshButton.addEventListener('click', () => {
    refreshTypedApi({source: 'manual'});
  });
  audioStartButton.addEventListener('click', () => mutateTyped('/api/audio/start', {}));
  audioStopButton.addEventListener('click', () => mutateTyped('/api/audio/stop', {}));
  masterGainInput.addEventListener('change', () => {
    mutateTyped('/api/master/gain', {gain: Number(masterGainInput.value)});
  });
  sessionNameInput.addEventListener('input', () => {
    sessionNameDirty = sessionNameInput.value.trim() !== '';
  });
  sessionSaveButton.addEventListener('click', saveSession);
  sessionLoadButton.addEventListener('click', loadSession);

  document.addEventListener('visibilitychange', () => {
    setTypedRefreshStatus(document.hidden ? 'Auto-refresh slowed while this tab is hidden.' : 'Auto-refresh resumed.');
    scheduleTypedRefresh(document.hidden ? typedRefreshHiddenIntervalMs : 1000);
  });

  checkHealth();
  loadCommands();
  refreshTypedApi({source: 'initial'});
})();
