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
  const audioDeviceSelect = document.getElementById('audio-device-select');
  const audioDeviceStatus = document.getElementById('audio-device-status');
  const typedError = document.getElementById('typed-error');
  const masterGainInput = document.getElementById('master-gain-input');
  const masterGainValue = document.getElementById('master-gain-value');
  const masterFxSelect = document.getElementById('master-fx-select');
  const masterFxLoadButton = document.getElementById('master-fx-load-button');
  const masterFxRemoveButton = document.getElementById('master-fx-remove-button');
  const masterFxStatus = document.getElementById('master-fx-status');
  const tempoBpmInput = document.getElementById('tempo-bpm-input');
  const tempoSetButton = document.getElementById('tempo-set-button');
  const linkStartButton = document.getElementById('link-start-button');
  const linkStopButton = document.getElementById('link-stop-button');
  const linkTempoStatus = document.getElementById('link-tempo-status');
  const midiChannelSelect = document.getElementById('midi-channel-select');
  const midiSlotSelect = document.getElementById('midi-slot-select');
  const midiLinkButton = document.getElementById('midi-link-button');
  const midiCutButton = document.getElementById('midi-cut-button');
  const midiRoutingStatus = document.getElementById('midi-routing-status');
  const midiRoutingList = document.getElementById('midi-routing-list');
  const sessionNameInput = document.getElementById('session-name-input');
  const sessionSaveButton = document.getElementById('session-save-button');
  const sessionLoadButton = document.getElementById('session-load-button');
  const sessionControlStatus = document.getElementById('session-control-status');
  const sessionNameOptions = document.getElementById('session-name-options');
  const sessionOptionsStatus = document.getElementById('session-options-status');
  const flowStatus = document.getElementById('flow-status');
  const flowOutput = document.getElementById('flow-output');
  const statusFields = {
    daemon: document.getElementById('status-daemon'),
    audio: document.getElementById('status-audio'),
    session: document.getElementById('status-session'),
    link: document.getElementById('status-link'),
    midi: document.getElementById('status-midi'),
  };
  const slotsList = document.getElementById('slots-list');
  const slotsCount = document.getElementById('slots-count');
  const slotInfoPanel = document.getElementById('slot-info-panel');
  const slotInfoTitle = document.getElementById('slot-info-title');
  const slotInfoStatus = document.getElementById('slot-info-status');
  const slotInfoMeta = document.getElementById('slot-info-meta');
  const slotInfoParams = document.getElementById('slot-info-params');
  const slotInfoOutput = document.getElementById('slot-info-output');
  const slotInfoClose = document.getElementById('slot-info-close');
  const csrfToken = document.querySelector('meta[name="vcpi-csrf-token"]')?.content || '';

  let entryCount = 0;
  let commandInFlight = false;
  let typedMutationInFlight = false;
  let typedRefreshInFlight = false;
  let typedRefreshPromise = null;
  let typedRefreshTimer = 0;
  let audioDeviceDirty = false;
  let tempoBpmDirty = false;
  let sessionNameDirty = false;
  let slotInfoInFlight = false;
  let currentInfoSlot = '';
  let currentInfoMode = '';
  let latestTypedStatusData = {};
  let latestTypedSlotsData = {};
  let masterFxAvailableCount = 0;

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
    const masterFxUnavailable = disabled || masterFxAvailableCount === 0 || selectedMasterFx() == null;
    masterFxLoadButton.disabled = masterFxUnavailable;
    masterFxRemoveButton.disabled = masterFxUnavailable;
  }

  function updateSlotInfoControlsDisabled() {
    document.querySelectorAll('[data-slot-info-action]').forEach((control) => {
      control.disabled = slotInfoInFlight;
    });
    slotInfoClose.disabled = slotInfoInFlight;
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

  function setLinkTempoStatus(message) {
    linkTempoStatus.textContent = message;
  }

  function setAudioDeviceStatus(message) {
    audioDeviceStatus.textContent = message;
  }

  function setFlowStatus(message) {
    flowStatus.textContent = message;
  }

  function setMidiRoutingStatus(message) {
    midiRoutingStatus.textContent = message;
  }

  function setMasterFxStatus(message) {
    masterFxStatus.textContent = message;
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

  function normalizeMasterFxCount(audio) {
    const count = Number(firstPresent(audio, ['master_effects', 'master_fx', 'effects'], 0));
    return Number.isInteger(count) && count > 0 ? count : 0;
  }

  function selectedMasterFx() {
    return normalizeMidiNumber(masterFxSelect.value, 1, 9999);
  }

  function renderMasterFxControls(audio) {
    const count = normalizeMasterFxCount(audio);
    masterFxAvailableCount = count;
    const previousValue = selectedMasterFx() || 1;
    masterFxSelect.textContent = '';
    if (count === 0) {
      appendSelectOption(masterFxSelect, '1', 'FX 1');
      masterFxSelect.value = '1';
      setMasterFxStatus('No master FX reported by typed status.');
      updateTypedControlsDisabled();
      return;
    }
    for (let effect = 1; effect <= count; effect += 1) {
      appendSelectOption(masterFxSelect, String(effect), `FX ${effect}`);
    }
    masterFxSelect.value = String(Math.min(previousValue, count));
    if (!currentInfoMode.startsWith('master-fx')) {
      setMasterFxStatus(`${count} master ${count === 1 ? 'FX' : 'FX'} available.`);
    }
    updateTypedControlsDisabled();
  }

  function audioDeviceValue(device) {
    if (typeof device === 'number' && Number.isFinite(device)) {
      return String(device);
    }
    if (typeof device === 'string') {
      return device.trim();
    }
    if (!device || typeof device !== 'object' || Array.isArray(device)) {
      return '';
    }
    const id = firstPresent(device, ['id', 'index', 'device', 'value'], undefined);
    if (typeof id === 'number' && Number.isFinite(id)) {
      return String(id);
    }
    if (typeof id === 'string' && id.trim()) {
      return id.trim();
    }
    const name = firstPresent(device, ['name', 'label', 'display_name'], '');
    return typeof name === 'string' ? name.trim() : '';
  }

  function audioDeviceLabel(device) {
    if (typeof device === 'number' && Number.isFinite(device)) {
      return `Device ${device}`;
    }
    if (typeof device === 'string') {
      return device.trim();
    }
    if (!device || typeof device !== 'object' || Array.isArray(device)) {
      return '';
    }
    const name = firstPresent(device, ['name', 'label', 'display_name'], '');
    const index = firstPresent(device, ['index', 'id'], undefined);
    const hostApi = firstPresent(device, ['hostapi_name', 'host_api', 'api'], '');
    const base = typeof name === 'string' && name.trim() ? name.trim() : audioDeviceValue(device);
    const suffix = typeof index === 'number' && Number.isFinite(index) ? ` #${index}` : '';
    const prefix = typeof hostApi === 'string' && hostApi.trim() ? `${hostApi.trim()} · ` : '';
    return base ? `${prefix}${base}${suffix}` : '';
  }

  function normalizeAudioDevices(data) {
    const source = data && typeof data === 'object'
      ? data.devices || data.outputs || data.output_devices || data.items || data
      : data;
    if (source === data && data && typeof data === 'object' && !Array.isArray(data) && Object.prototype.hasOwnProperty.call(data, 'ok')) {
      return [];
    }
    const rawDevices = Array.isArray(source)
      ? source
      : source && typeof source === 'object'
        ? Object.keys(source).map((key) => {
          const device = source[key];
          return device && typeof device === 'object' && !Array.isArray(device)
            ? {id: key, ...device}
            : {id: key, name: String(device)};
        })
        : [];
    const seen = new Set();
    const devices = [];
    rawDevices.forEach((device) => {
      const value = audioDeviceValue(device);
      const label = audioDeviceLabel(device);
      if (!value || seen.has(value)) {
        return;
      }
      seen.add(value);
      devices.push({
        value,
        label: label || value,
        selected: device && typeof device === 'object' && !Array.isArray(device) ? device.selected === true : false,
        default: device && typeof device === 'object' && !Array.isArray(device) ? device.default === true : false,
      });
    });
    return devices;
  }

  function currentAudioDevice(audio, status, devicesData) {
    const devices = normalizeAudioDevices(devicesData);
    const selectedDevice = devices.find((device) => device.selected) || devices.find((device) => device.default);
    if (selectedDevice) {
      return selectedDevice.value;
    }
    const fromAudio = firstPresent(
      audio,
      ['device', 'output_device', 'output', 'selected_device', 'current_device', 'device_name'],
      undefined,
    );
    const fromStatus = firstPresent(status, ['audio_device', 'output_device'], undefined);
    const devicesObject = asObject(devicesData);
    const fromDevices = firstPresent(devicesObject, ['current', 'selected', 'active', 'device', 'output_device'], undefined);
    return audioDeviceValue(fromAudio) || audioDeviceValue(fromStatus) || audioDeviceValue(fromDevices);
  }

  function appendAudioDeviceOption(value, label) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    audioDeviceSelect.append(option);
  }

  function renderAudioDevices(data, statusData) {
    const status = asObject(statusData.status || statusData);
    const audio = asObject(status.audio);
    const devices = normalizeAudioDevices(data);
    const previousValue = audioDeviceSelect.value;
    const currentDevice = currentAudioDevice(audio, status, data);
    const preservingUserChoice = document.activeElement === audioDeviceSelect || audioDeviceDirty;
    const selectedValue = preservingUserChoice ? previousValue : currentDevice || previousValue;

    audioDeviceSelect.textContent = '';
    appendAudioDeviceOption('', 'System default');
    devices.forEach((device) => {
      appendAudioDeviceOption(device.value, device.label);
    });

    if (selectedValue && !devices.some((device) => device.value === selectedValue)) {
      appendAudioDeviceOption(selectedValue, currentDevice === selectedValue ? `Current: ${selectedValue}` : `Selected: ${selectedValue}`);
    }

    audioDeviceSelect.value = selectedValue || '';
    const currentText = currentDevice ? `Current ${currentDevice}` : 'Current device not reported';
    const countText = devices.length === 1 ? '1 output available' : `${devices.length} outputs available`;
    setAudioDeviceStatus(`${currentText} · ${countText}`);
  }

  function renderAudioDevicesUnavailable(statusData) {
    const status = asObject(statusData.status || statusData);
    const audio = asObject(status.audio);
    const currentDevice = currentAudioDevice(audio, status, {});
    const previousValue = audioDeviceSelect.value;
    const selectedValue = previousValue || currentDevice;

    audioDeviceSelect.textContent = '';
    appendAudioDeviceOption('', 'System default');
    if (selectedValue) {
      appendAudioDeviceOption(selectedValue, currentDevice === selectedValue ? `Current: ${selectedValue}` : `Selected: ${selectedValue}`);
    }
    audioDeviceSelect.value = selectedValue || '';
    setAudioDeviceStatus(currentDevice ? `Current ${currentDevice} · Device list unavailable` : 'Device list unavailable; system default can still be used.');
  }

  function normalizeBpm(value, fallback) {
    const number = Number(value);
    if (!Number.isFinite(number)) {
      return fallback;
    }
    return Math.min(300, Math.max(20, number));
  }

  function normalizeMidiNumber(value, min, max) {
    const number = Number(value);
    if (!Number.isInteger(number) || number < min || number > max) {
      return null;
    }
    return number;
  }

  function appendSelectOption(select, value, label) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    select.append(option);
  }

  function selectedMidiChannel() {
    return normalizeMidiNumber(midiChannelSelect.value, 1, 16);
  }

  function selectedMidiSlot() {
    return normalizeMidiNumber(midiSlotSelect.value, 1, 8);
  }

  function renderLinkTempo(link, status) {
    const enabled = firstPresent(link, ['enabled', 'active', 'running'], firstPresent(status, ['link_enabled'], undefined));
    const bpm = firstPresent(link, ['bpm', 'tempo'], firstPresent(status, ['bpm', 'tempo'], undefined));
    const normalizedBpm = normalizeBpm(bpm, undefined);
    const linkState = formatValue(enabled, 'Unknown');
    const bpmText = normalizedBpm == null ? 'BPM not reported' : `${normalizedBpm.toFixed(1)} BPM`;

    setLinkTempoStatus(`Link ${linkState} · ${bpmText}`);
    linkStartButton.setAttribute('aria-pressed', enabled === true ? 'true' : 'false');
    linkStopButton.setAttribute('aria-pressed', enabled === false ? 'true' : 'false');
    linkStartButton.classList.toggle('typed-button--active', enabled === true);
    linkStopButton.classList.toggle('typed-button--active', enabled === false);

    if (normalizedBpm == null || document.activeElement === tempoBpmInput || tempoBpmDirty) {
      return;
    }
    tempoBpmInput.value = normalizedBpm.toFixed(1);
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

  function normalizeSessionName(value) {
    if (typeof value === 'string') {
      return value.trim();
    }
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      return '';
    }
    const name = firstPresent(value, ['name', 'session', 'filename', 'file', 'id'], '');
    return typeof name === 'string' ? name.trim() : '';
  }

  function normalizeSessions(data) {
    const source = data.sessions || data.items || data.names || data;
    let sessions = [];
    if (Array.isArray(source)) {
      sessions = source.map(normalizeSessionName);
    } else if (source && typeof source === 'object') {
      sessions = Object.keys(source).map((key) => normalizeSessionName(source[key]) || key);
    }

    return Array.from(new Set(sessions.filter((name) => name && isSafeSessionName(name)))).sort((left, right) => left.localeCompare(right));
  }

  function renderSessions(data) {
    const sessions = normalizeSessions(data);
    sessionNameOptions.textContent = '';
    sessions.forEach((name) => {
      const option = document.createElement('option');
      option.value = name;
      sessionNameOptions.append(option);
    });
    sessionOptionsStatus.textContent = `${sessions.length} saved ${sessions.length === 1 ? 'session' : 'sessions'} available.`;
  }

  function renderSessionsUnavailable() {
    sessionNameOptions.textContent = '';
    sessionOptionsStatus.textContent = 'Saved session suggestions unavailable; manual entry still works.';
  }

  function signalFlowText(data) {
    if (typeof data === 'string') {
      return data;
    }
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      return '';
    }
    return normalizeOutput(firstPresent(data, ['flow', 'ascii', 'diagram', 'output', 'text'], ''));
  }

  function renderSignalFlow(data) {
    const flow = signalFlowText(data).trimEnd();
    flowOutput.textContent = flow || 'Signal-flow endpoint returned no diagram.';
    setFlowStatus(flow ? 'Typed signal flow loaded.' : 'No signal-flow diagram returned.');
  }

  function renderSignalFlowUnavailable() {
    flowOutput.textContent = 'Signal-flow diagnostics are unavailable. Status and slots can still refresh.';
    setFlowStatus('Signal-flow endpoint unavailable.');
  }

  function midiSlotLabel(slotNumber, slots) {
    const match = slots.find((slot) => normalizeMidiNumber(firstPresent(slot, ['slot', 'index', 'number', 'id'], null), 1, 8) === slotNumber);
    if (!match) {
      return `Slot ${slotNumber}`;
    }
    const loaded = match.loaded !== false;
    const name = formatValue(firstPresent(match, ['name', 'plugin', 'instrument', 'type'], ''), '');
    if (loaded && name) {
      return `Slot ${slotNumber} — ${name}`;
    }
    return loaded ? `Slot ${slotNumber} — loaded` : `Slot ${slotNumber} — empty`;
  }

  function syncMidiRoutingSelectors(slotsData) {
    const slots = normalizeSlots(slotsData);
    const selectedChannelValue = midiChannelSelect.value || '1';
    const selectedSlotValue = midiSlotSelect.value || '1';

    midiChannelSelect.textContent = '';
    for (let channel = 1; channel <= 16; channel += 1) {
      appendSelectOption(midiChannelSelect, String(channel), `Ch ${channel}`);
    }

    midiSlotSelect.textContent = '';
    for (let slotNumber = 1; slotNumber <= 8; slotNumber += 1) {
      appendSelectOption(midiSlotSelect, String(slotNumber), midiSlotLabel(slotNumber, slots));
    }

    midiChannelSelect.value = normalizeMidiNumber(selectedChannelValue, 1, 16) == null ? '1' : selectedChannelValue;
    midiSlotSelect.value = normalizeMidiNumber(selectedSlotValue, 1, 8) == null ? '1' : selectedSlotValue;
  }

  function addMidiRoute(routes, channel, slot) {
    const normalizedChannel = normalizeMidiNumber(channel, 1, 16);
    const normalizedSlot = normalizeMidiNumber(slot, 1, 8);
    if (normalizedChannel == null || normalizedSlot == null) {
      return;
    }
    const existing = routes.find((route) => route.channel === normalizedChannel);
    if (existing) {
      existing.slot = normalizedSlot;
    } else {
      routes.push({channel: normalizedChannel, slot: normalizedSlot});
    }
  }

  function addMidiRoutesForSlot(routes, slotNumber, channels) {
    if (Array.isArray(channels)) {
      channels.forEach((channel) => addMidiRoute(routes, channel, slotNumber));
      return;
    }
    if (typeof channels === 'string') {
      channels.split(',').forEach((channel) => addMidiRoute(routes, channel.trim(), slotNumber));
      return;
    }
    addMidiRoute(routes, channels, slotNumber);
  }

  function routingFromStatus(statusData) {
    const status = asObject(statusData.status || statusData);
    const midi = asObject(status.midi);
    const routing = firstPresent(midi, ['routing', 'routes', 'channel_routes', 'channels'], firstPresent(status, ['midi_routing'], undefined));
    const routes = [];

    if (Array.isArray(routing)) {
      routing.forEach((entry) => {
        if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
          return;
        }
        addMidiRoute(routes, firstPresent(entry, ['channel', 'ch', 'midi_channel'], null), firstPresent(entry, ['slot', 'slot_id', 'target'], null));
      });
    } else if (routing && typeof routing === 'object') {
      Object.keys(routing).forEach((channel) => {
        addMidiRoute(routes, channel, routing[channel]);
      });
    }

    return routes;
  }

  function routingFromSlots(slotsData) {
    const routes = [];
    normalizeSlots(slotsData).forEach((rawSlot, index) => {
      const slot = asObject(rawSlot);
      const slotNumber = normalizeMidiNumber(firstPresent(slot, ['slot', 'index', 'number', 'id'], index + 1), 1, 8);
      if (slotNumber == null) {
        return;
      }
      const channels = firstPresent(slot, ['midi_channels', 'midi', 'channel', 'midi_channel'], undefined);
      if (channels == null || channels === '—') {
        return;
      }
      addMidiRoutesForSlot(routes, slotNumber, channels);
    });
    return routes;
  }

  function currentMidiRoutes(statusData, slotsData) {
    const statusRoutes = routingFromStatus(statusData);
    const routes = statusRoutes.length > 0 ? statusRoutes : routingFromSlots(slotsData);
    return routes.sort((left, right) => left.channel - right.channel);
  }

  function renderMidiRouting(statusData, slotsData) {
    syncMidiRoutingSelectors(slotsData);
    const slots = normalizeSlots(slotsData);
    const routes = currentMidiRoutes(statusData, slotsData);
    midiRoutingList.textContent = '';

    if (routes.length === 0) {
      midiRoutingList.append(makeEmptyState('No MIDI channel routing is currently reported.'));
      setMidiRoutingStatus('No channel routes reported. Link a channel to a slot to add one.');
      return;
    }

    routes.forEach((route) => {
      const item = document.createElement('div');
      item.className = 'midi-routing-item';

      const channel = document.createElement('strong');
      channel.textContent = `Ch ${route.channel}`;

      const target = document.createElement('span');
      target.textContent = midiSlotLabel(route.slot, slots);

      item.append(channel, target);
      midiRoutingList.append(item);
    });

    setMidiRoutingStatus(`${routes.length} ${routes.length === 1 ? 'route' : 'routes'} active.`);
  }

  async function linkMidiRoute() {
    const channel = selectedMidiChannel();
    const slot = selectedMidiSlot();
    if (channel == null || slot == null) {
      setMidiRoutingStatus('Choose a MIDI channel and slot before linking.');
      showTypedError('Choose a MIDI channel and slot before linking.');
      return;
    }
    setMidiRoutingStatus(`Linking channel ${channel} to slot ${slot}…`);
    const ok = await mutateTyped('/api/midi/link', {channel, slot});
    setMidiRoutingStatus(ok ? `Channel ${channel} linked to slot ${slot}. Status refreshed.` : 'MIDI link failed. See typed API status above.');
  }

  async function cutMidiRoute() {
    const channel = selectedMidiChannel();
    if (channel == null) {
      setMidiRoutingStatus('Choose a MIDI channel before cutting.');
      showTypedError('Choose a MIDI channel before cutting.');
      return;
    }
    setMidiRoutingStatus(`Cutting MIDI channel ${channel}…`);
    const ok = await mutateTyped('/api/midi/cut', {channel});
    setMidiRoutingStatus(ok ? `Channel ${channel} cut. Status refreshed.` : 'MIDI cut failed. See typed API status above.');
  }

  function slotInfoText(data) {
    if (typeof data === 'string') {
      return data;
    }
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      return '';
    }
    return normalizeOutput(firstPresent(data, ['rendered', 'info', 'text', 'output', 'details', 'description', 'message'], ''));
  }

  function slotInfoMetadata(data) {
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      return {};
    }
    const metadata = asObject(firstPresent(data, ['metadata', 'meta', 'instrument', 'plugin', 'slot'], {}));
    if (Object.keys(metadata).length > 0) {
      return metadata;
    }
    const excluded = new Set(['ok', 'rendered', 'info', 'text', 'output', 'details', 'description', 'message', 'error', 'params', 'parameters', 'items', 'rows', 'ascii', 'knobs']);
    const shallow = {};
    Object.keys(data).forEach((key) => {
      const value = data[key];
      if (!excluded.has(key) && value != null && typeof value !== 'object') {
        shallow[key] = value;
      }
    });
    return shallow;
  }

  function resetSlotParamsTable() {
    slotInfoParams.textContent = '';
    slotInfoParams.hidden = true;
  }

  function slotParamsText(data) {
    if (typeof data === 'string') {
      return data;
    }
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      return '';
    }
    return normalizeOutput(firstPresent(data, ['rendered', 'ascii', 'knobs', 'output', 'text'], ''));
  }

  function parameterRowsFromSource(source, groupLabel, target) {
    const rows = [];
    const targetInfo = asObject(target);
    const addRow = (name, value, group) => {
      const valueObject = asObject(value);
      const displayName = firstPresent(valueObject, ['name', 'label', 'id', 'key'], name);
      const displayValue = Object.keys(valueObject).length > 0
        ? firstPresent(valueObject, ['display', 'display_value', 'value', 'current', 'normalized', 'default'], '')
        : value;
      const units = firstPresent(valueObject, ['units', 'unit'], '');
      const type = firstPresent(valueObject, ['type', 'kind'], '');
      const minimum = firstPresent(valueObject, ['min', 'minimum', 'lo', 'low'], undefined);
      const maximum = firstPresent(valueObject, ['max', 'maximum', 'hi', 'high'], undefined);
      const step = firstPresent(valueObject, ['step', 'increment'], undefined);
      const range = minimum != null || maximum != null ? `${formatValue(minimum, '—')}–${formatValue(maximum, '—')}` : '—';
      const paramName = formatValue(displayName, '').trim();
      const currentValue = firstPresent(valueObject, ['value', 'current'], displayValue);
      rows.push({
        group: formatValue(group, 'Instrument'),
        name: paramName || 'Unnamed parameter',
        value: units ? `${formatValue(displayValue, '—')} ${units}` : formatValue(displayValue, '—'),
        range: type && range === '—' ? formatValue(type, '—') : range,
        paramName,
        currentValue,
        minimum,
        maximum,
        step,
        targetKind: firstPresent(targetInfo, ['targetKind'], 'instrument'),
        effectIndex: firstPresent(targetInfo, ['effectIndex'], undefined),
      });
    };

    const collect = (value, group) => {
      if (Array.isArray(value)) {
        value.forEach((item, index) => {
          if (item && typeof item === 'object' && !Array.isArray(item)) {
            const nested = firstPresent(item, ['params', 'parameters', 'items', 'rows'], undefined);
            if (nested != null && !firstPresent(item, ['value', 'current', 'display', 'display_value', 'normalized', 'default'], undefined)) {
              collect(nested, formatValue(firstPresent(item, ['name', 'label', 'component', 'plugin', 'type'], group), group));
              return;
            }
          }
          addRow(String(index + 1), item, group);
        });
        return;
      }
      if (value && typeof value === 'object') {
        const nested = firstPresent(value, ['params', 'parameters', 'items', 'rows'], undefined);
        if (nested != null && value === source) {
          collect(nested, group);
          return;
        }
        Object.keys(value).forEach((key) => {
          const item = value[key];
          if (item && typeof item === 'object' && !Array.isArray(item)) {
            const nestedItem = firstPresent(item, ['params', 'parameters', 'items', 'rows'], undefined);
            if (nestedItem != null && !firstPresent(item, ['value', 'current', 'display', 'display_value', 'normalized', 'default'], undefined)) {
              collect(nestedItem, formatValue(firstPresent(item, ['name', 'label', 'component', 'plugin', 'type'], key), key));
              return;
            }
          }
          addRow(key, item, group);
        });
      }
    };

    collect(source, groupLabel);
    return rows;
  }

  function slotParameterRows(data) {
    if (Array.isArray(data)) {
      return parameterRowsFromSource(data, 'Instrument');
    }
    if (!data || typeof data !== 'object') {
      return [];
    }
    const rows = [];
    const topLevelParams = firstPresent(data, ['params', 'parameters', 'items', 'rows'], undefined);
    if (topLevelParams != null) {
      rows.push(...parameterRowsFromSource(topLevelParams, 'Instrument', {targetKind: 'instrument'}));
    }
    const instrument = asObject(data.instrument || data.plugin);
    const instrumentParams = firstPresent(instrument, ['params', 'parameters', 'items', 'rows'], undefined);
    if (instrumentParams != null) {
      rows.push(...parameterRowsFromSource(instrumentParams, formatValue(firstPresent(instrument, ['name', 'label', 'type'], 'Instrument'), 'Instrument'), {targetKind: 'instrument'}));
    }
    const effects = Array.isArray(data.effects) ? data.effects : [];
    effects.forEach((effect, index) => {
      const effectObject = asObject(effect);
      const effectParams = firstPresent(effectObject, ['params', 'parameters', 'items', 'rows'], undefined);
      if (effectParams != null) {
        const effectIndex = firstPresent(effectObject, ['index', 'effect', 'effectIndex'], index + 1);
        rows.push(...parameterRowsFromSource(
          effectParams,
          formatValue(firstPresent(effectObject, ['name', 'label', 'type'], `Effect ${index + 1}`), `Effect ${index + 1}`),
          {targetKind: 'effect', effectIndex},
        ));
      }
    });
    return rows;
  }

  function masterFxParameterRows(data) {
    if (Array.isArray(data)) {
      return parameterRowsFromSource(data, 'Master FX', {targetKind: 'master_effect'});
    }
    if (!data || typeof data !== 'object') {
      return [];
    }
    const effect = asObject(data.effect || data.plugin);
    const effectName = formatValue(firstPresent(effect, ['name', 'label', 'type'], 'Master FX'), 'Master FX');
    const source = firstPresent(effect, ['params', 'parameters', 'items', 'rows'], firstPresent(data, ['params', 'parameters', 'items', 'rows'], undefined));
    return source == null ? [] : parameterRowsFromSource(source, effectName, {targetKind: 'master_effect'});
  }

  function appendHeaderCell(row, label) {
    const cell = document.createElement('th');
    cell.scope = 'col';
    cell.textContent = label;
    row.append(cell);
  }

  function appendParamCell(row, value) {
    const cell = document.createElement('td');
    cell.textContent = value;
    row.append(cell);
  }

  function isFiniteNumericParamValue(value) {
    return typeof value === 'number' && Number.isFinite(value);
  }

  function optionalFiniteNumericParamValue(value) {
    if (value == null) {
      return true;
    }
    return isFiniteNumericParamValue(value);
  }

  function editableSlotParam(param) {
    return Boolean(
      param.paramName
        && isFiniteNumericParamValue(param.currentValue)
        && optionalFiniteNumericParamValue(param.minimum)
        && optionalFiniteNumericParamValue(param.maximum),
    );
  }

  async function applySlotParam(slotNumber, slotName, param, input) {
    const value = Number(input.value);
    if (!Number.isFinite(value)) {
      showTypedError(`Enter a finite numeric value for ${param.paramName}.`);
      input.focus();
      return;
    }

    const payload = {
      name: param.paramName,
      value,
    };
    const targetLabel = param.targetKind === 'effect' ? `FX ${param.effectIndex}` : 'instrument';
    if (param.targetKind === 'effect') {
      const effectIndex = Number(param.effectIndex);
      if (!Number.isInteger(effectIndex) || effectIndex < 1) {
        showTypedError(`Cannot apply ${param.paramName}: effect index is unavailable.`);
        return;
      }
      payload.target = 'effect';
      payload.effect = effectIndex;
    }

    slotInfoStatus.textContent = `Applying ${param.paramName} on slot ${slotNumber} ${targetLabel}…`;
    const ok = await mutateTyped(`/api/slots/${encodeURIComponent(slotNumber)}/params`, payload);
    if (ok) {
      await loadSlotParams(slotNumber, slotName);
    } else {
      slotInfoStatus.textContent = `Could not apply ${param.paramName}. See typed API status above.`;
    }
  }

  async function applyMasterFxParam(effectNumber, param, input) {
    const value = Number(input.value);
    if (!Number.isFinite(value)) {
      showTypedError(`Enter a finite numeric value for ${param.paramName}.`);
      input.focus();
      return;
    }

    slotInfoStatus.textContent = `Applying ${param.paramName} on master FX ${effectNumber}…`;
    const ok = await mutateTyped(`/api/master/fx/${encodeURIComponent(effectNumber)}/params`, {
      name: param.paramName,
      value,
    });
    if (ok) {
      await loadMasterFxParams(effectNumber);
    } else {
      slotInfoStatus.textContent = `Could not apply ${param.paramName}. See typed API status above.`;
    }
  }

  async function removeMasterFx(effectNumber) {
    if (!effectNumber || masterFxAvailableCount === 0) {
      setMasterFxStatus('Choose a loaded master FX before removing.');
      showTypedError('Choose a loaded master FX before removing.');
      return;
    }
    if (!window.confirm(`Remove master FX ${effectNumber}?`)) {
      return;
    }

    setMasterFxStatus(`Removing master FX ${effectNumber}…`);
    const ok = await mutateTyped(`/api/master/fx/${encodeURIComponent(effectNumber)}/clear`, {});
    if (!ok) {
      setMasterFxStatus('Master FX remove failed. See typed API status above.');
      return;
    }

    if (!currentInfoMode.startsWith('master-fx')) {
      setMasterFxStatus(`Removed master FX ${effectNumber}. Status refreshed.`);
      return;
    }

    if (masterFxAvailableCount > 0 && effectNumber <= masterFxAvailableCount) {
      await loadMasterFxParams(effectNumber);
      return;
    }

    renderSlotInfoUnavailable(`master FX ${effectNumber}`, `Master FX ${effectNumber} was removed.`);
    setMasterFxStatus(`Removed master FX ${effectNumber}.`);
  }

  async function removeSlotFx(slotNumber, slotName, effectIndex, groupName) {
    const effectNumber = normalizeMidiNumber(effectIndex, 1, 9999);
    if (effectNumber == null) {
      showTypedError('Cannot remove slot FX: effect index is unavailable.');
      return;
    }
    const label = groupName ? `FX ${effectNumber} (${groupName})` : `FX ${effectNumber}`;
    if (!window.confirm(`Remove slot ${slotNumber} ${label}?`)) {
      return;
    }

    slotInfoStatus.textContent = `Removing slot ${slotNumber} ${label}…`;
    const ok = await mutateTyped(`/api/slots/${encodeURIComponent(slotNumber)}/fx/${encodeURIComponent(effectNumber)}/clear`, {});
    if (ok) {
      await loadSlotParams(slotNumber, slotName);
    } else {
      slotInfoStatus.textContent = `Could not remove slot ${slotNumber} ${label}. See typed API status above.`;
    }
  }

  function appendParamEditCell(row, slotNumber, slotName, param) {
    const cell = document.createElement('td');
    if (!editableSlotParam(param)) {
      cell.textContent = '—';
      cell.title = 'This parameter is read-only in the dashboard.';
      row.append(cell);
      return;
    }

    const form = document.createElement('form');
    form.className = 'slot-param-edit';
    const targetLabel = param.targetKind === 'effect' ? `slot ${slotNumber} FX ${param.effectIndex}` : `slot ${slotNumber}`;
    const labelText = `Set ${param.paramName} for ${targetLabel}`;
    const input = document.createElement('input');
    input.type = 'number';
    input.inputMode = 'decimal';
    input.value = String(param.currentValue);
    input.dataset.typedAction = 'true';
    input.setAttribute('aria-label', labelText);
    input.title = labelText;
    if (isFiniteNumericParamValue(param.minimum)) {
      input.min = String(param.minimum);
    }
    if (isFiniteNumericParamValue(param.maximum)) {
      input.max = String(param.maximum);
    }
    if (isFiniteNumericParamValue(param.step)) {
      input.step = String(param.step);
    } else {
      input.step = 'any';
    }

    const button = document.createElement('button');
    button.type = 'submit';
    button.className = 'typed-button typed-button--ghost slot-param-apply';
    button.textContent = 'Apply';
    button.dataset.typedAction = 'true';
    button.setAttribute('aria-label', `Apply ${param.paramName} to ${targetLabel}`);
    button.title = `Apply ${param.paramName}`;

    form.addEventListener('submit', (event) => {
      event.preventDefault();
      applySlotParam(slotNumber, slotName, param, input);
    });
    form.append(input, button);
    cell.append(form);
    row.append(cell);
  }

  function appendMasterFxParamEditCell(row, effectNumber, param) {
    const cell = document.createElement('td');
    if (!editableSlotParam(param)) {
      cell.textContent = '—';
      cell.title = 'This parameter is read-only in the dashboard.';
      row.append(cell);
      return;
    }

    const form = document.createElement('form');
    form.className = 'slot-param-edit';
    const labelText = `Set ${param.paramName} for master FX ${effectNumber}`;
    const input = document.createElement('input');
    input.type = 'number';
    input.inputMode = 'decimal';
    input.value = String(param.currentValue);
    input.dataset.typedAction = 'true';
    input.setAttribute('aria-label', labelText);
    input.title = labelText;
    if (isFiniteNumericParamValue(param.minimum)) {
      input.min = String(param.minimum);
    }
    if (isFiniteNumericParamValue(param.maximum)) {
      input.max = String(param.maximum);
    }
    input.step = isFiniteNumericParamValue(param.step) ? String(param.step) : 'any';

    const button = document.createElement('button');
    button.type = 'submit';
    button.className = 'typed-button typed-button--ghost slot-param-apply';
    button.textContent = 'Apply';
    button.dataset.typedAction = 'true';
    button.setAttribute('aria-label', `Apply ${param.paramName} to master FX ${effectNumber}`);
    button.title = `Apply ${param.paramName}`;

    form.addEventListener('submit', (event) => {
      event.preventDefault();
      applyMasterFxParam(effectNumber, param, input);
    });
    form.append(input, button);
    cell.append(form);
    row.append(cell);
  }

  function appendSlotFxRemoveRow(tbody, slotNumber, slotName, param) {
    const row = document.createElement('tr');
    const labelCell = document.createElement('td');
    labelCell.colSpan = 4;
    labelCell.textContent = `Slot FX ${param.effectIndex} · ${param.group}`;
    const actionCell = document.createElement('td');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'typed-button typed-button--ghost typed-button--danger slot-param-apply';
    button.textContent = 'Remove FX';
    button.dataset.typedAction = 'true';
    button.setAttribute('aria-label', `Remove slot ${slotNumber} FX ${param.effectIndex}`);
    button.addEventListener('click', () => {
      removeSlotFx(slotNumber, slotName, param.effectIndex, param.group);
    });
    actionCell.append(button);
    row.append(labelCell, actionCell);
    tbody.append(row);
  }

  function renderSlotParamsTable(rows, slotNumber, slotName) {
    slotInfoParams.textContent = '';
    if (rows.length === 0) {
      slotInfoParams.hidden = true;
      return;
    }

    const table = document.createElement('table');
    table.className = 'slot-params-table';
    const thead = document.createElement('thead');
    const headerRow = document.createElement('tr');
    appendHeaderCell(headerRow, 'Component');
    appendHeaderCell(headerRow, 'Parameter');
    appendHeaderCell(headerRow, 'Value');
    appendHeaderCell(headerRow, 'Range');
    appendHeaderCell(headerRow, 'Edit');
    thead.append(headerRow);

    const tbody = document.createElement('tbody');
    const visibleEffectGroups = new Set();
    rows.forEach((param) => {
      if (param.targetKind === 'effect' && param.effectIndex != null && !visibleEffectGroups.has(String(param.effectIndex))) {
        visibleEffectGroups.add(String(param.effectIndex));
        appendSlotFxRemoveRow(tbody, slotNumber, slotName, param);
      }
      const row = document.createElement('tr');
      appendParamCell(row, param.group);
      appendParamCell(row, param.name);
      appendParamCell(row, param.value);
      appendParamCell(row, param.range);
      appendParamEditCell(row, slotNumber, slotName, param);
      tbody.append(row);
    });

    table.append(thead, tbody);
    slotInfoParams.append(table);
    slotInfoParams.hidden = false;
  }

  function renderMasterFxParamsTable(rows, effectNumber) {
    slotInfoParams.textContent = '';
    if (rows.length === 0) {
      slotInfoParams.hidden = true;
      return;
    }

    const table = document.createElement('table');
    table.className = 'slot-params-table';
    const thead = document.createElement('thead');
    const headerRow = document.createElement('tr');
    appendHeaderCell(headerRow, 'Component');
    appendHeaderCell(headerRow, 'Parameter');
    appendHeaderCell(headerRow, 'Value');
    appendHeaderCell(headerRow, 'Range');
    appendHeaderCell(headerRow, 'Edit');
    thead.append(headerRow);

    const tbody = document.createElement('tbody');
    rows.forEach((param) => {
      const row = document.createElement('tr');
      appendParamCell(row, param.group);
      appendParamCell(row, param.name);
      appendParamCell(row, param.value);
      appendParamCell(row, param.range);
      appendMasterFxParamEditCell(row, effectNumber, param);
      tbody.append(row);
    });

    table.append(thead, tbody);
    slotInfoParams.append(table);
    slotInfoParams.hidden = false;
  }

  function renderSlotInfoMetadata(metadata) {
    slotInfoMeta.textContent = '';
    const keys = Object.keys(metadata);
    if (keys.length === 0) {
      slotInfoMeta.hidden = true;
      return;
    }
    keys.forEach((key) => {
      appendSlotMeta(slotInfoMeta, key.replace(/_/g, ' '), metadata[key]);
    });
    slotInfoMeta.hidden = false;
  }

  function setSlotInfoPanelVisible(visible) {
    slotInfoPanel.hidden = !visible;
  }

  function renderSlotInfo(slotNumber, slotName, data) {
    const text = slotInfoText(data).trimEnd();
    const metadata = slotInfoMetadata(data);
    currentInfoSlot = String(slotNumber);
    currentInfoMode = 'info';
    slotInfoTitle.textContent = `Slot ${slotNumber} info`;
    slotInfoStatus.textContent = slotName ? `Loaded details for ${slotName}.` : 'Loaded slot details.';
    renderSlotInfoMetadata(metadata);
    resetSlotParamsTable();
    slotInfoOutput.textContent = text || 'The slot info endpoint returned no details for this loaded slot.';
    setSlotInfoPanelVisible(true);
  }

  function renderSlotInfoUnavailable(slotNumber, message) {
    currentInfoSlot = String(slotNumber);
    currentInfoMode = '';
    slotInfoTitle.textContent = `Slot ${slotNumber} details`;
    slotInfoStatus.textContent = message || 'Slot details unavailable.';
    renderSlotInfoMetadata({});
    resetSlotParamsTable();
    slotInfoOutput.textContent = message || 'Slot details unavailable.';
    setSlotInfoPanelVisible(true);
  }

  async function loadSlotInfo(slotNumber, slotName) {
    if (!slotNumber || slotInfoInFlight) {
      return;
    }
    slotInfoInFlight = true;
    updateSlotInfoControlsDisabled();
    showTypedError('');
    setSlotInfoPanelVisible(true);
    currentInfoSlot = String(slotNumber);
    currentInfoMode = 'info';
    slotInfoTitle.textContent = `Slot ${slotNumber} info`;
    slotInfoStatus.textContent = `Loading read-only info for slot ${slotNumber}…`;
    renderSlotInfoMetadata({});
    resetSlotParamsTable();
    slotInfoOutput.textContent = 'Inspecting plugin details…';
    try {
      const data = await fetchJson(`/api/slots/${encodeURIComponent(slotNumber)}/info`, {headers: {'Accept': 'application/json'}});
      renderSlotInfo(slotNumber, slotName, data);
    } catch (error) {
      const message = `Slot info unavailable: ${error.message || String(error)}`;
      renderSlotInfoUnavailable(slotNumber, message);
      showTypedError(message);
    } finally {
      slotInfoInFlight = false;
      updateSlotInfoControlsDisabled();
    }
  }


  function renderSlotParams(slotNumber, slotName, data) {
    const rows = slotParameterRows(data);
    const text = slotParamsText(data).trimEnd();
    currentInfoSlot = String(slotNumber);
    currentInfoMode = 'params';
    slotInfoTitle.textContent = `Slot ${slotNumber} params`;
    slotInfoStatus.textContent = slotName ? `Loaded parameters for ${slotName}.` : 'Loaded parameters.';
    renderSlotInfoMetadata(slotInfoMetadata(data));
    renderSlotParamsTable(rows, slotNumber, slotName);
    slotInfoOutput.textContent = text || (rows.length > 0 ? 'No rendered parameter diagram supplied by the backend.' : 'The params endpoint returned no parameters for this loaded slot.');
    setSlotInfoPanelVisible(true);
  }

  async function loadSlotParams(slotNumber, slotName) {
    if (!slotNumber || slotInfoInFlight) {
      return;
    }
    slotInfoInFlight = true;
    updateSlotInfoControlsDisabled();
    showTypedError('');
    setSlotInfoPanelVisible(true);
    currentInfoSlot = String(slotNumber);
    currentInfoMode = 'params';
    slotInfoTitle.textContent = `Slot ${slotNumber} params`;
    slotInfoStatus.textContent = `Loading params for slot ${slotNumber}…`;
    renderSlotInfoMetadata({});
    resetSlotParamsTable();
    slotInfoOutput.textContent = 'Inspecting plugin parameters…';
    try {
      const data = await fetchJson(`/api/slots/${encodeURIComponent(slotNumber)}/params`, {headers: {'Accept': 'application/json'}});
      renderSlotParams(slotNumber, slotName, data);
    } catch (error) {
      const message = `Slot params unavailable: ${error.message || String(error)}`;
      renderSlotInfoUnavailable(slotNumber, message);
      showTypedError(message);
    } finally {
      slotInfoInFlight = false;
      updateSlotInfoControlsDisabled();
    }
  }

  function renderMasterFxParams(effectNumber, data) {
    const rows = masterFxParameterRows(data);
    const text = slotParamsText(data).trimEnd();
    currentInfoSlot = `master-fx-${effectNumber}`;
    currentInfoMode = 'master-fx-params';
    slotInfoTitle.textContent = `Master FX ${effectNumber} params`;
    slotInfoStatus.textContent = `Loaded parameters for master FX ${effectNumber}.`;
    setMasterFxStatus(`Loaded master FX ${effectNumber} parameters.`);
    renderSlotInfoMetadata(slotInfoMetadata(data));
    renderMasterFxParamsTable(rows, effectNumber);
    slotInfoOutput.textContent = text || (rows.length > 0 ? 'No rendered parameter diagram supplied by the backend.' : 'The params endpoint returned no parameters for this master FX.');
    setSlotInfoPanelVisible(true);
  }

  async function loadMasterFxParams(effectNumber) {
    if (!effectNumber || slotInfoInFlight) {
      return;
    }
    slotInfoInFlight = true;
    updateSlotInfoControlsDisabled();
    showTypedError('');
    setSlotInfoPanelVisible(true);
    currentInfoSlot = `master-fx-${effectNumber}`;
    currentInfoMode = 'master-fx-params';
    slotInfoTitle.textContent = `Master FX ${effectNumber} params`;
    slotInfoStatus.textContent = `Loading params for master FX ${effectNumber}…`;
    setMasterFxStatus(`Loading master FX ${effectNumber} params…`);
    renderSlotInfoMetadata({});
    resetSlotParamsTable();
    slotInfoOutput.textContent = 'Inspecting master FX parameters…';
    try {
      const data = await fetchJson(`/api/master/fx/${encodeURIComponent(effectNumber)}/params`, {headers: {'Accept': 'application/json'}});
      renderMasterFxParams(effectNumber, data);
    } catch (error) {
      const message = `Master FX params unavailable: ${error.message || String(error)}`;
      renderSlotInfoUnavailable(`master FX ${effectNumber}`, message);
      setMasterFxStatus(message);
      showTypedError(message);
    } finally {
      slotInfoInFlight = false;
      updateSlotInfoControlsDisabled();
    }
  }

  function renderStatus(data) {
    latestTypedStatusData = data;
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
    renderMasterFxControls(audio);
    renderLinkTempo(link, status);
    renderMidiRouting(latestTypedStatusData, latestTypedSlotsData);
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

  function slotInfoButton(slotNumber, slotName) {
    const selected = currentInfoSlot === String(slotNumber) && currentInfoMode === 'info';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = selected ? 'typed-button typed-button--info typed-button--active' : 'typed-button typed-button--ghost typed-button--info';
    button.textContent = 'Info';
    button.setAttribute('aria-pressed', selected ? 'true' : 'false');
    button.dataset.slotInfoAction = 'true';
    button.addEventListener('click', () => {
      loadSlotInfo(slotNumber, slotName);
    });
    return button;
  }


  function slotParamsButton(slotNumber, slotName) {
    const selected = currentInfoSlot === String(slotNumber) && currentInfoMode === 'params';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = selected ? 'typed-button typed-button--info typed-button--active' : 'typed-button typed-button--ghost typed-button--info';
    button.textContent = 'Params';
    button.setAttribute('aria-pressed', selected ? 'true' : 'false');
    button.dataset.slotInfoAction = 'true';
    button.addEventListener('click', () => {
      loadSlotParams(slotNumber, slotName);
    });
    return button;
  }

  async function auditionSlot(slotNumber) {
    setTypedRefreshStatus(`Auditioning slot ${slotNumber}…`);
    const ok = await mutateTyped(`/api/slots/${encodeURIComponent(slotNumber)}/note`, {
      note: 60,
      velocity: 100,
      duration_ms: 300,
    });
    setTypedRefreshStatus(ok ? `Auditioned slot ${slotNumber}. Status refreshed.` : 'Audition failed. See typed API status above.');
  }

  function renderSlots(data) {
    latestTypedSlotsData = data;
    const slots = normalizeSlots(data);
    slotsList.textContent = '';
    slotsCount.textContent = `${slots.length} ${slots.length === 1 ? 'slot' : 'slots'}`;

    if (slots.length === 0) {
      slotsList.append(makeEmptyState('No slots returned by the typed API.'));
      return;
    }

    const loadedSlotNumbers = new Set();

    slots.forEach((rawSlot, index) => {
      const slot = asObject(rawSlot);
      const slotNumber = firstPresent(slot, ['slot', 'index', 'number', 'id'], index + 1);
      const gain = firstPresent(slot, ['gain', 'volume', 'level'], 1);
      const muted = Boolean(firstPresent(slot, ['muted', 'mute'], false));
      const soloed = Boolean(firstPresent(slot, ['soloed', 'solo'], false));
      const loaded = slot.loaded !== false;
      const slotName = formatValue(firstPresent(slot, ['name', 'plugin', 'instrument', 'type'], 'Loaded'), 'Loaded');
      if (loaded) {
        loadedSlotNumbers.add(String(slotNumber));
      }

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
          slotInfoButton(slotNumber, slotName),
          slotParamsButton(slotNumber, slotName),
          typedActionButton('Audition', false, () => {
            auditionSlot(slotNumber);
          }),
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
    if (currentInfoSlot && currentInfoMode !== 'master-fx-params' && !loadedSlotNumbers.has(currentInfoSlot) && !slotInfoInFlight) {
      renderSlotInfoUnavailable(currentInfoSlot, `Slot ${currentInfoSlot} is empty or no longer loaded.`);
    }
    updateSlotInfoControlsDisabled();
    renderMidiRouting(latestTypedStatusData, latestTypedSlotsData);
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
    setTypedRefreshStatus(source === 'auto' ? 'Auto-refreshing status, slots, sessions, audio devices, and signal flow…' : 'Refreshing status, slots, sessions, audio devices, and signal flow…');
    if (source !== 'auto') {
      typedRefreshButton.textContent = 'Refreshing…';
    }

    typedRefreshPromise = (async () => {
      const results = await Promise.allSettled([
        fetchJson('/api/status', {headers: {'Accept': 'application/json'}}),
        fetchJson('/api/slots', {headers: {'Accept': 'application/json'}}),
        fetchJson('/api/sessions', {headers: {'Accept': 'application/json'}}),
        fetchJson('/api/audio/devices', {headers: {'Accept': 'application/json'}}),
        fetchJson('/api/flow', {headers: {'Accept': 'application/json'}}),
      ]);

      const messages = [];
      if (results[0].status === 'fulfilled') {
        renderStatus(results[0].value);
      } else {
        Object.values(statusFields).forEach((field) => {
          field.textContent = 'Typed endpoint unavailable';
        });
        latestTypedStatusData = {};
        masterGainValue.textContent = 'Typed endpoint unavailable';
        messages.push(results[0].reason.message || String(results[0].reason));
      }

      if (results[1].status === 'fulfilled') {
        renderSlots(results[1].value);
      } else {
        latestTypedSlotsData = {};
        slotsList.textContent = '';
        slotsList.append(makeEmptyState('Typed slot endpoint is unavailable. The command console still works below.'));
        slotsCount.textContent = 'No typed slots';
        messages.push(results[1].reason.message || String(results[1].reason));
      }
      renderMidiRouting(latestTypedStatusData, latestTypedSlotsData);

      if (results[2].status === 'fulfilled') {
        renderSessions(results[2].value);
      } else {
        renderSessionsUnavailable();
      }

      if (results[3].status === 'fulfilled') {
        const statusData = results[0].status === 'fulfilled' ? results[0].value : {};
        renderAudioDevices(results[3].value, statusData);
      } else {
        const statusData = results[0].status === 'fulfilled' ? results[0].value : {};
        renderAudioDevicesUnavailable(statusData);
      }

      if (results[4].status === 'fulfilled') {
        renderSignalFlow(results[4].value);
      } else {
        renderSignalFlowUnavailable();
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

  function audioStartPayloadFromInput() {
    const device = audioDeviceSelect.value.trim();
    return device ? {device} : {};
  }

  async function startAudio() {
    const payload = audioStartPayloadFromInput();
    const target = payload.device ? ` on ${payload.device}` : ' with the system default';
    setAudioDeviceStatus(`Starting audio${target}…`);
    const ok = await mutateTyped('/api/audio/start', payload);
    if (ok) {
      audioDeviceDirty = false;
      setAudioDeviceStatus('Audio started. Status refreshed.');
    } else {
      setAudioDeviceStatus('Audio start failed. See typed API status above.');
    }
  }

  async function stopAudio() {
    setAudioDeviceStatus('Stopping audio…');
    const ok = await mutateTyped('/api/audio/stop', {});
    setAudioDeviceStatus(ok ? 'Audio stopped. Status refreshed.' : 'Audio stop failed. See typed API status above.');
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

  function validateTempoBpm() {
    const rawBpm = tempoBpmInput.value.trim();
    const bpm = Number(rawBpm);
    if (!rawBpm || !Number.isFinite(bpm)) {
      return {message: 'Enter a BPM between 20 and 300.'};
    }
    if (bpm < 20 || bpm > 300) {
      return {message: 'Tempo must be between 20 and 300 BPM.'};
    }
    return {bpm: Math.round(bpm * 10) / 10, message: ''};
  }

  function optionalTempoPayload() {
    if (!tempoBpmInput.value.trim()) {
      return {payload: {}, message: ''};
    }
    const validation = validateTempoBpm();
    if (validation.message) {
      return {payload: null, message: validation.message};
    }
    return {payload: {bpm: validation.bpm}, message: ''};
  }

  async function setTempo() {
    const validation = validateTempoBpm();
    if (validation.message) {
      setLinkTempoStatus(validation.message);
      showTypedError(validation.message);
      tempoBpmInput.focus();
      return;
    }
    setLinkTempoStatus(`Setting tempo to ${validation.bpm.toFixed(1)} BPM…`);
    const ok = await mutateTyped('/api/tempo', {bpm: validation.bpm});
    if (ok) {
      tempoBpmDirty = false;
      setLinkTempoStatus(`Tempo set to ${validation.bpm.toFixed(1)} BPM. Status refreshed.`);
    } else {
      setLinkTempoStatus('Tempo update failed. See typed API status above.');
    }
  }

  async function startLink() {
    const tempo = optionalTempoPayload();
    if (tempo.message) {
      setLinkTempoStatus(tempo.message);
      showTypedError(tempo.message);
      tempoBpmInput.focus();
      return;
    }
    setLinkTempoStatus('Starting Ableton Link…');
    const ok = await mutateTyped('/api/link/start', tempo.payload || {});
    setLinkTempoStatus(ok ? 'Ableton Link started. Status refreshed.' : 'Ableton Link start failed. See typed API status above.');
  }

  async function stopLink() {
    setLinkTempoStatus('Stopping Ableton Link…');
    const ok = await mutateTyped('/api/link/stop', {});
    setLinkTempoStatus(ok ? 'Ableton Link stopped. Status refreshed.' : 'Ableton Link stop failed. See typed API status above.');
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
  audioStartButton.addEventListener('click', startAudio);
  audioStopButton.addEventListener('click', stopAudio);
  audioDeviceSelect.addEventListener('change', () => {
    audioDeviceDirty = true;
  });
  masterGainInput.addEventListener('change', () => {
    mutateTyped('/api/master/gain', {gain: Number(masterGainInput.value)});
  });
  masterFxLoadButton.addEventListener('click', () => {
    const effect = selectedMasterFx();
    if (effect == null) {
      setMasterFxStatus('Choose a valid master FX index.');
      showTypedError('Choose a valid master FX index.');
      return;
    }
    loadMasterFxParams(effect);
  });
  masterFxRemoveButton.addEventListener('click', () => {
    const effect = selectedMasterFx();
    if (effect == null) {
      setMasterFxStatus('Choose a valid master FX index.');
      showTypedError('Choose a valid master FX index.');
      return;
    }
    removeMasterFx(effect);
  });
  tempoBpmInput.addEventListener('input', () => {
    tempoBpmDirty = tempoBpmInput.value.trim() !== '';
  });
  tempoSetButton.addEventListener('click', setTempo);
  linkStartButton.addEventListener('click', startLink);
  linkStopButton.addEventListener('click', stopLink);
  midiLinkButton.addEventListener('click', linkMidiRoute);
  midiCutButton.addEventListener('click', cutMidiRoute);
  sessionNameInput.addEventListener('input', () => {
    sessionNameDirty = sessionNameInput.value.trim() !== '';
  });
  sessionSaveButton.addEventListener('click', saveSession);
  sessionLoadButton.addEventListener('click', loadSession);
  slotInfoClose.addEventListener('click', () => {
    currentInfoSlot = '';
    currentInfoMode = '';
    setSlotInfoPanelVisible(false);
    updateSlotInfoControlsDisabled();
  });

  document.addEventListener('visibilitychange', () => {
    setTypedRefreshStatus(document.hidden ? 'Auto-refresh slowed while this tab is hidden.' : 'Auto-refresh resumed.');
    scheduleTypedRefresh(document.hidden ? typedRefreshHiddenIntervalMs : 1000);
  });

  checkHealth();
  loadCommands();
  refreshTypedApi({source: 'initial'});
})();
