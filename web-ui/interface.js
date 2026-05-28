// Global state
let isKeyboardEnabled = false;
let isRobotEngaged = false;
let currentConfig = {};
let warningTimeout = null;

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
    })
    .catch(() => {});
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
