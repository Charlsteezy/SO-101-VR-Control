// Global state
let isKeyboardEnabled = false;
let isRobotEngaged = false;
let isRecording = false;
let camerasConnected = false;
let discoveredCameras = [];
let previewCameraNames = [];
let previewTimer = null;
let currentConfig = {};
let warningTimeout = null;
let recordWarningTimeout = null;

// Settings modal
function openSettings() {
  document.getElementById('settingsModal').classList.add('show');
  loadConfiguration();
}

function closeSettings() {
  document.getElementById('settingsModal').classList.remove('show');
}

function loadConfiguration() {
  fetch('/api/config')
    .then(r => r.json())
    .then(config => {
      currentConfig = config;
      populateSettingsForm(config);
    })
    .catch(() => {});
}

function populateSettingsForm(config) {
  document.getElementById('leftArmName').value = config.robot?.left_arm?.name || '';
  document.getElementById('leftArmPort').value = config.robot?.left_arm?.port || '';
  document.getElementById('httpsPort').value = config.network?.https_port || '';
  document.getElementById('websocketPort').value = config.network?.websocket_port || '';
  document.getElementById('vrScale').value = config.robot?.vr_to_robot_scale || '';
  document.getElementById('sendInterval').value = (config.robot?.send_interval * 1000) || '';
}

function restartSystem() {
  if (!confirm('Restart the system? This will temporarily disconnect.')) return;
  const btn = document.getElementById('restartButton');
  btn.disabled = true;
  btn.textContent = 'Restarting...';

  fetch('/api/restart', { method: 'POST', headers: { 'Content-Type': 'application/json' } })
    .then(r => {
      if (r.ok) {
        closeSettings();
        setTimeout(() => window.location.reload(), 5000);
      }
    })
    .finally(() => {
      btn.disabled = false;
      btn.textContent = 'Restart';
    });
}

function saveConfiguration() {
  const form = document.getElementById('settingsForm');
  const fd = new FormData(form);

  const updatedConfig = {
    robot: {
      left_arm: { name: fd.get('leftArmName'), port: fd.get('leftArmPort'), enabled: true },
      right_arm: { name: 'Disabled', port: '/dev/ttyACM_NONE', enabled: false },
      vr_to_robot_scale: parseFloat(fd.get('vrScale')),
      send_interval: parseFloat(fd.get('sendInterval')) / 1000
    },
    network: {
      https_port: parseInt(fd.get('httpsPort')),
      websocket_port: parseInt(fd.get('websocketPort'))
    }
  };

  const btn = document.getElementById('saveButton');
  btn.disabled = true;
  btn.textContent = 'Saving...';

  fetch('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updatedConfig)
  })
    .then(r => r.json())
    .then(data => {
      if (data.success) alert('Saved. Use Restart to apply.');
    })
    .finally(() => {
      btn.disabled = false;
      btn.textContent = 'Save';
    });
}

// Status polling
function updateStatus() {
  fetch('/api/status')
    .then(r => r.json())
    .then(data => {
      const armOk = data.left_arm_connected || data.right_arm_connected;
      document.getElementById('leftArmStatus').className = 'status-indicator' + (armOk ? ' connected' : '');
      document.getElementById('vrStatus').className = 'status-indicator' + (data.vrConnected ? ' connected' : '');

      isKeyboardEnabled = data.keyboardEnabled;
      const kbCard = document.querySelector('.keyboard-card');
      const kbBtn = document.getElementById('keyboardToggleBtn');
      const kbText = document.getElementById('keyboardToggleText');
      if (isKeyboardEnabled) {
        kbCard.classList.add('active');
        kbBtn.classList.add('active');
        kbText.textContent = 'Disable';
      } else {
        kbCard.classList.remove('active');
        kbBtn.classList.remove('active');
        kbText.textContent = 'Enable';
      }

      if (data.robotEngaged !== undefined) {
        isRobotEngaged = data.robotEngaged;
        updateEngagementUI();
      }

      camerasConnected = !!data.cameraConnected;
      isRecording = !!data.recording;
      updateRecordingUI(data);
      updateCameraPreview(data);
    })
    .catch(() => {});
}

function updateCameraPreview(data) {
  const section = document.getElementById('cameraPreview');
  const inner = document.getElementById('cameraPreviewInner');
  if (!section || !inner) return;

  const connected = data.connectedCameras || {};
  const names = Object.keys(connected);

  if (!data.cameraConnected || names.length === 0) {
    stopCameraPreview();
    section.hidden = true;
    inner.innerHTML = '';
    previewCameraNames = [];
    return;
  }

  const namesKey = names.join('|');
  const prevKey = previewCameraNames.join('|');
  if (namesKey !== prevKey) {
    previewCameraNames = names;
    inner.innerHTML = '';
    names.forEach(name => {
      const tile = document.createElement('div');
      tile.className = 'camera-tile';
      tile.innerHTML =
        `<div class="camera-tile-label">${name}</div>` +
        `<img id="preview-${name}" alt="${name} camera" decoding="async">`;
      inner.appendChild(tile);
    });
  }

  section.hidden = false;
  startCameraPreview();
}

function startCameraPreview() {
  if (previewTimer) return;
  refreshCameraPreviewFrames();
  previewTimer = setInterval(refreshCameraPreviewFrames, 250);
}

function stopCameraPreview() {
  if (previewTimer) {
    clearInterval(previewTimer);
    previewTimer = null;
  }
}

function refreshCameraPreviewFrames() {
  if (!camerasConnected || previewCameraNames.length === 0) return;
  const bust = Date.now();
  previewCameraNames.forEach(name => {
    const img = document.getElementById(`preview-${name}`);
    if (!img) return;
    img.src = `/api/cameras/${encodeURIComponent(name)}/frame.jpg?t=${bust}`;
  });
}

function updateRecordingUI(data) {
  const btn = document.getElementById('recordBtn');
  const text = document.getElementById('recordBtnText');
  const discard = document.getElementById('discardBtn');
  const status = document.getElementById('recordStatusText');
  const recDot = document.getElementById('recStatus');
  const recordingEnabled = data.recordingEnabled !== false;

  if (!btn || !text || !discard || !status) return;

  if (isRecording) {
    btn.classList.add('recording');
    text.textContent = 'Stop Recording';
    discard.disabled = false;
    const ep = data.episodeIndex ?? 0;
    const frames = data.frameCount ?? 0;
    status.textContent = `Recording ep ${ep} · ${frames} frames`;
    status.style.color = 'var(--red)';
    if (recDot) recDot.className = 'pill-dot recording';
  } else {
    btn.classList.remove('recording');
    text.textContent = 'Start Recording';
    discard.disabled = true;
    const ep = data.episodeIndex ?? 0;
    if (recordingEnabled && !camerasConnected) {
      status.textContent = 'Cameras not connected';
      status.style.color = 'var(--amber)';
    } else {
      status.textContent = ep > 0 ? `Idle · ${ep} episode(s) saved` : 'Idle';
      status.style.color = 'var(--text-muted)';
    }
    if (recDot) recDot.className = 'pill-dot' + (camerasConnected ? ' connected' : '');
  }

  btn.disabled = !recordingEnabled && !isRecording;
}

function showRecordWarning(message) {
  const warn = document.getElementById('recordWarning');
  if (!warn) return;
  warn.textContent = message;
  warn.classList.add('show');
  if (recordWarningTimeout) clearTimeout(recordWarningTimeout);
  recordWarningTimeout = setTimeout(() => warn.classList.remove('show'), 5000);
}

function renderCameraResults(cameras) {
  const list = document.getElementById('cameraSearchResults');
  const applyBtn = document.getElementById('applyCamerasBtn');
  if (!list) return;

  list.innerHTML = '';
  if (!cameras || cameras.length === 0) {
    list.innerHTML = '<li class="camera-empty">No cameras found</li>';
    if (applyBtn) applyBtn.disabled = true;
    return;
  }

  cameras.forEach((cam, i) => {
    const profile = cam.default_stream_profile || {};
    const li = document.createElement('li');
    li.innerHTML =
      `<span class="cam-id">${cam.id}</span>` +
      `<span class="cam-meta">${profile.width || '?'}×${profile.height || '?'} @ ${profile.fps || '?'}fps</span>`;
    if (i < 3) li.classList.add('will-use');
    list.appendChild(li);
  });
  if (applyBtn) applyBtn.disabled = false;
}

function searchCameras() {
  const btn = document.getElementById('searchCamerasBtn');
  const applyBtn = document.getElementById('applyCamerasBtn');
  if (isRecording) {
    showRecordWarning('Stop recording before searching for cameras');
    return;
  }
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Searching...';
  }
  if (applyBtn) applyBtn.disabled = true;

  fetch('/api/cameras/search')
    .then(async r => {
      const data = await r.json().catch(() => ({}));
      if (!r.ok || data.success === false) {
        throw new Error(data.error || 'Camera search failed');
      }
      discoveredCameras = data.cameras || [];
      renderCameraResults(discoveredCameras);
      camerasConnected = false;
      updateRecordingUI({
        recording: false,
        cameraConnected: false,
        recordingEnabled: true,
        episodeIndex: 0,
        frameCount: 0,
      });
      updateCameraPreview({ cameraConnected: false, connectedCameras: {} });
      if (discoveredCameras.length === 0) {
        showRecordWarning('No USB cameras detected');
      }
    })
    .catch(err => {
      discoveredCameras = [];
      renderCameraResults([]);
      showRecordWarning(err.message || 'Camera search failed');
    })
    .finally(() => {
      if (btn) {
        btn.disabled = false;
        btn.textContent = 'Search Cameras';
      }
    });
}

function applyCameras() {
  const btn = document.getElementById('applyCamerasBtn');
  if (isRecording) {
    showRecordWarning('Stop recording before connecting cameras');
    return;
  }
  if (!discoveredCameras.length) {
    showRecordWarning('Search for cameras first');
    return;
  }
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Connecting...';
  }

  fetch('/api/cameras/apply', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cameras: discoveredCameras })
  })
    .then(async r => {
      const data = await r.json().catch(() => ({}));
      if (!r.ok || data.success === false) {
        throw new Error(data.error || 'Failed to connect cameras');
      }
      camerasConnected = !!data.cameraConnected;
      const names = (data.assignments || []).map(a => `${a.name}→${a.id}`).join(', ');
      showRecordWarning(camerasConnected ? `Connected: ${names}` : 'Camera connect incomplete');
      const connectedMap = {};
      (data.assignments || []).forEach(a => { connectedMap[a.name] = String(a.id); });
      updateCameraPreview({
        cameraConnected: camerasConnected,
        connectedCameras: connectedMap,
      });
      updateStatus();
    })
    .catch(err => showRecordWarning(err.message || 'Failed to connect cameras'))
    .finally(() => {
      if (btn) {
        btn.disabled = !discoveredCameras.length;
        btn.textContent = 'Connect Found';
      }
    });
}

function toggleRecording() {
  if (isRecording) {
    postRecordingAction('stop');
    return;
  }
  if (!isRobotEngaged) {
    showRecordWarning('Connect the robot before recording');
    showConnectionWarning();
    return;
  }
  if (!camerasConnected) {
    showRecordWarning('Cameras are not connected — click Search Cameras');
    return;
  }
  postRecordingAction('start');
}

function discardRecording() {
  if (!isRecording) return;
  if (!confirm('Discard the current episode without saving?')) return;
  postRecordingAction('discard');
}

function postRecordingAction(action) {
  fetch('/api/recording', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action })
  })
    .then(async r => {
      const data = await r.json().catch(() => ({}));
      if (!r.ok || data.success === false) {
        showRecordWarning(data.error || `Recording ${action} failed`);
        return;
      }
      if (action === 'start') {
        isRecording = true;
        updateRecordingUI({ recording: true, episodeIndex: 0, frameCount: 0, cameraConnected: camerasConnected, recordingEnabled: true });
      } else if (action === 'stop' || action === 'discard') {
        isRecording = false;
        updateRecordingUI({ recording: false, episodeIndex: 0, frameCount: 0, cameraConnected: camerasConnected, recordingEnabled: true });
      }
      updateStatus();
    })
    .catch(() => showRecordWarning(`Recording ${action} failed`));
}

function updateEngagementUI() {
  const btn = document.getElementById('robotEngageBtn');
  const text = document.getElementById('engageBtnText');
  const status = document.getElementById('engagementStatusText');
  const warn = document.getElementById('connectionWarning');

  if (isRobotEngaged) {
    btn.classList.add('disconnect');
    btn.classList.remove('needs-attention');
    text.textContent = 'Disconnect Robot';
    status.textContent = 'Motors Engaged';
    status.style.color = 'var(--accent)';
    if (warn) warn.classList.remove('show');
  } else {
    btn.classList.remove('disconnect');
    text.textContent = 'Connect Robot';
    status.textContent = 'Motors Disengaged';
    status.style.color = 'var(--text-muted)';
  }
}

function showConnectionWarning() {
  const btn = document.getElementById('robotEngageBtn');
  const warn = document.getElementById('connectionWarning');
  if (!isRobotEngaged) {
    btn.classList.add('needs-attention');
    if (warn) warn.classList.add('show');
    if (warningTimeout) clearTimeout(warningTimeout);
    warningTimeout = setTimeout(() => {
      btn.classList.remove('needs-attention');
      if (warn) warn.classList.remove('show');
    }, 5000);
  }
}

function toggleRobotEngagement() {
  const action = isRobotEngaged ? 'disconnect' : 'connect';
  fetch('/api/robot', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action })
  })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        isRobotEngaged = !isRobotEngaged;
        updateEngagementUI();
      }
    })
    .catch(() => {});
}

function toggleKeyboardControl() {
  const action = isKeyboardEnabled ? 'disable' : 'enable';
  fetch('/api/keyboard', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action })
  })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        isKeyboardEnabled = !isKeyboardEnabled;
        const kbCard = document.querySelector('.keyboard-card');
        const kbBtn = document.getElementById('keyboardToggleBtn');
        const kbText = document.getElementById('keyboardToggleText');
        if (isKeyboardEnabled) {
          kbCard.classList.add('active');
          kbBtn.classList.add('active');
          kbText.textContent = 'Disable';
        } else {
          kbCard.classList.remove('active');
          kbBtn.classList.remove('active');
          kbText.textContent = 'Enable';
        }
      }
    })
    .catch(() => {});
}

// VR mode detection
function isVRMode() {
  return window.navigator.xr && document.fullscreenElement;
}

function updateUIForDevice() {
  const ui = document.getElementById('desktopInterface');
  if (isVRMode()) {
    ui.style.display = 'none';
  } else if (navigator.xr) {
    navigator.xr.isSessionSupported('immersive-vr').then(supported => {
      ui.style.display = supported ? 'none' : 'block';
    }).catch(() => { ui.style.display = 'block'; });
  } else {
    ui.style.display = 'block';
  }
}

// Keyboard handler
let pressedKeys = new Set();
document.addEventListener('keydown', handleKeyDown, { capture: true });
document.addEventListener('keyup', handleKeyUp, { capture: true });

function handleKeyDown(e) {
  if (isControlKey(e.code)) e.preventDefault();
  if (isControlKey(e.code) && !isRobotEngaged) { showConnectionWarning(); return; }
  if (!isKeyboardEnabled || pressedKeys.has(e.code)) return;
  if (isControlKey(e.code)) { pressedKeys.add(e.code); sendKeyCommand(e.code, 'press'); }
}

function handleKeyUp(e) {
  if (isControlKey(e.code)) e.preventDefault();
  if (!isKeyboardEnabled || !pressedKeys.has(e.code)) return;
  if (isControlKey(e.code)) { pressedKeys.delete(e.code); sendKeyCommand(e.code, 'release'); }
}

function isControlKey(code) {
  return ['KeyW','KeyS','KeyA','KeyD','KeyQ','KeyE','KeyZ','KeyX','KeyR','KeyT','KeyF','Tab','Escape'].includes(code);
}

function sendKeyCommand(keyCode, action) {
  const map = {
    'KeyW':'w','KeyS':'s','KeyA':'a','KeyD':'d','KeyQ':'q','KeyE':'e',
    'KeyZ':'z','KeyX':'x','KeyR':'r','KeyT':'t','KeyF':'f',
    'Tab':'tab','Escape':'esc'
  };
  const key = map[keyCode];
  if (!key) return;
  fetch('/api/keypress', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key, action })
  }).catch(() => {});
}

// View switching
function switchToVrView() {
  document.getElementById('desktopInterface').style.display = 'none';
  if (!document.getElementById('start-tracking-button')) createFallbackVrButton();
  showBackToDesktopButton();
}

function switchToDesktopView() {
  document.getElementById('desktopInterface').style.display = 'block';
  hideBackToDesktopButton();
}

function showBackToDesktopButton() {
  let btn = document.getElementById('back-to-desktop-button');
  if (!btn) {
    btn = document.createElement('button');
    btn.id = 'back-to-desktop-button';
    btn.textContent = '← Back';
    btn.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);padding:10px 24px;font-size:14px;background:#222;color:#aaa;border:1px solid #333;border-radius:8px;cursor:pointer;z-index:9998;';
    btn.onclick = switchToDesktopView;
    document.body.appendChild(btn);
  }
  btn.style.display = 'block';
}

function hideBackToDesktopButton() {
  const btn = document.getElementById('back-to-desktop-button');
  if (btn) btn.style.display = 'none';
}

function createFallbackVrButton() {
  if (typeof createVrInstructionsPanel === 'function') createVrInstructionsPanel();
  const btn = document.createElement('button');
  btn.id = 'start-tracking-button';
  btn.textContent = 'Start VR Control';
  btn.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);padding:18px 40px;font-size:18px;font-weight:700;background:#76b900;color:#000;border:none;border-radius:8px;cursor:pointer;z-index:9999;';
  btn.onclick = async () => {
    const scene = document.querySelector('a-scene');
    if (!scene) return;
    btn.textContent = 'Connecting...';
    btn.disabled = true;
    try {
      const sr = await fetch('/api/status');
      const st = await sr.json();
      if (!st.robotEngaged) {
        btn.textContent = 'Engaging Motors...';
        const cr = await fetch('/api/robot', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'connect'}) });
        const cd = await cr.json();
        if (!cd.success) throw new Error(cd.error || 'Failed');
        await new Promise(r => setTimeout(r, 500));
      }
      btn.textContent = 'Starting VR...';
      await scene.enterVR(true);
    } catch (err) {
      alert('Failed: ' + err.message);
      btn.textContent = 'Start VR Control';
      btn.disabled = false;
    }
  };
  document.body.appendChild(btn);
}

// Init
document.addEventListener('DOMContentLoaded', () => {
  updateUIForDevice();
  updateStatus();
  setInterval(updateStatus, 2000);
  document.addEventListener('fullscreenchange', updateUIForDevice);
  document.getElementById('settingsForm').addEventListener('submit', e => { e.preventDefault(); saveConfiguration(); });
  document.getElementById('settingsModal').addEventListener('click', e => { if (e.target.id === 'settingsModal') closeSettings(); });
});

window.addEventListener('resize', updateUIForDevice);
