(() => {
  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || '';
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const formatYear = (value) => ({ '1': 'First Year', '2': 'Second Year', '3': 'Third Year', '4': 'Fourth Year' }[String(value ?? '').trim()] || String(value ?? ''));
  const api = async (url, options = {}) => {
    const headers = new Headers(options.headers || {});
    headers.set('X-CSRFToken', csrf());
    if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    const response = await fetch(url, { ...options, headers });
    const contentType = response.headers.get('content-type') || '';
    const body = contentType.includes('application/json') ? await response.json() : await response.text();
    if (!response.ok) throw new Error(body.error || body.message || `Request failed (${response.status})`);
    return body;
  };
  const setMessage = (el, message, kind = '') => {
    if (!el) return;
    el.textContent = message || '';
    el.className = el.className.replace(/\b(success|error|info)\b/g, '').trim();
    if (kind) el.classList.add(kind);
  };
  const cameraError = (error) => {
    if (error?.name === 'NotAllowedError') return 'Camera permission was denied. Allow camera access in your browser settings and try again.';
    if (error?.name === 'NotFoundError') return 'No camera was found. Connect a webcam or choose another camera in browser settings.';
    if (error?.name === 'NotReadableError') return 'The camera is already in use by another app. Close other camera apps and retry.';
    return error?.message || 'Could not start the camera. Check browser permissions and try again.';
  };

  const menuButton = document.getElementById('mobile-menu');
  menuButton?.addEventListener('click', () => document.getElementById('sidebar')?.classList.toggle('sidebar-open'));

  const studentSearch = document.getElementById('student-search');
  studentSearch?.addEventListener('input', () => {
    const query = studentSearch.value.trim().toLowerCase();
    document.querySelectorAll('.student-row').forEach((row) => {
      row.hidden = !row.dataset.search.includes(query);
    });
  });

  const reportMode = document.getElementById('report-mode');
  const updateReportFields = () => {
    if (!reportMode) return;
    const mode = reportMode.value;
    document.querySelectorAll('.class-filter').forEach((el) => el.classList.toggle('hidden', mode === 'student'));
    document.querySelectorAll('.student-filter').forEach((el) => el.classList.toggle('hidden', mode !== 'student'));
  };
  reportMode?.addEventListener('change', updateReportFields);
  updateReportFields();

  // Browser-camera enrollment; the hardware bridge supplies the card UID.
  const enrollmentForm = document.getElementById('student-enrollment-form');
  if (enrollmentForm) {
    const video = document.getElementById('enroll-video');
    const placeholder = document.getElementById('enroll-camera-placeholder');
    const enableCameraButton = document.getElementById('enable-enroll-camera');
    const captureButton = document.getElementById('capture-photo');
    const clearButton = document.getElementById('clear-photos');
    const submitButton = document.getElementById('submit-enrollment');
    const uidInput = document.getElementById('student-uid');
    const messageEl = document.getElementById('enrollment-message');
    const countEl = document.getElementById('photo-count');
    const progressEl = document.getElementById('photo-progress');
    const thumbnails = document.getElementById('photo-thumbnails');
    const scanButton = document.getElementById('scan-enrollment-card');
    const scanHint = document.getElementById('tag-scan-hint');
    let stream = null;
    let photos = [];
    let scanTimer = null;
    let scanListening = false;
    let scanCursor = 0;

    const refreshPhotos = () => {
      countEl.innerHTML = `${photos.length} <span>/ 3</span>`;
      progressEl.style.width = `${Math.min(100, photos.length / 3 * 100)}%`;
      clearButton.disabled = photos.length === 0;
      submitButton.disabled = photos.length < 3 || !uidInput.value.trim();
      thumbnails.replaceChildren();
      if (!photos.length) {
        const empty = document.createElement('span'); empty.className = 'photo-empty'; empty.textContent = 'Your captured photos appear here.'; thumbnails.append(empty);
      }
      photos.forEach((src, i) => {
        const figure = document.createElement('figure'); figure.className = 'photo-thumb';
        const image = document.createElement('img'); image.src = src; image.alt = `Captured face photo ${i + 1}`;
        const caption = document.createElement('figcaption'); caption.textContent = `Photo 0${i + 1}`;
        figure.append(image, caption); thumbnails.append(figure);
      });
    };
    uidInput.addEventListener('input', refreshPhotos);
    enableCameraButton.addEventListener('click', async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 640 }, height: { ideal: 480 }, facingMode: 'user' }, audio: false });
        video.srcObject = stream; placeholder.classList.add('hidden'); captureButton.disabled = false;
        enableCameraButton.textContent = 'Camera enabled'; enableCameraButton.disabled = true;
        setMessage(messageEl, 'Camera ready. Capture three clear photos, changing your angle slightly.', 'info');
      } catch (error) { setMessage(messageEl, cameraError(error), 'error'); }
    });
    captureButton.addEventListener('click', async () => {
      if (!stream || video.readyState < 2) { setMessage(messageEl, 'Wait for the camera preview, then capture.', 'error'); return; }
      if (photos.length >= 8) { setMessage(messageEl, 'You have reached the maximum of eight photos.', 'error'); return; }
      const canvas = document.createElement('canvas');
      canvas.width = Math.min(video.videoWidth || 640, 640);
      canvas.height = Math.round(canvas.width * ((video.videoHeight || 480) / (video.videoWidth || 640)));
      const context = canvas.getContext('2d', { willReadFrequently: false });
      context.drawImage(video, 0, 0, canvas.width, canvas.height);
      photos.push(canvas.toDataURL('image/jpeg', 0.84));
      refreshPhotos();
      setMessage(messageEl, `Captured photo ${photos.length}. ${photos.length < 3 ? `Capture ${3 - photos.length} more.` : 'Ready to save the enrollment.'}`, 'success');
    });
    clearButton.addEventListener('click', () => { photos = []; refreshPhotos(); setMessage(messageEl, 'Captured photos cleared.'); });

    scanButton.addEventListener('click', async () => {
      if (scanListening) return;
      scanButton.disabled = true;
      try {
        const c = await api('/api/scans/cursor'); scanCursor = c.cursor;
        scanListening = true;
        scanButton.textContent = 'Waiting for card…';
        setMessage(scanHint, 'Present the new, unassigned card to the RC522 reader.', 'info');
        const poll = async () => {
          if (!scanListening) return;
          try {
            const result = await api(`/api/scans?after_id=${scanCursor}`);
            for (const event of result.events || []) {
              scanCursor = Math.max(scanCursor, event.ID);
              if (event.ID > c.cursor) {
                uidInput.value = event.UID; refreshPhotos();
                setMessage(scanHint, `Scanned NFC UID ${event.UID}. Verify it is the intended card.`, 'success');
                scanListening = false; scanButton.disabled = false; scanButton.textContent = '⌁ Scan card';
                return;
              }
            }
          } catch (error) { setMessage(scanHint, error.message, 'error'); }
          scanTimer = window.setTimeout(poll, 700);
        };
        poll();
      } catch (error) { scanListening = false; scanButton.disabled = false; scanButton.textContent = '⌁ Scan card'; setMessage(scanHint, error.message, 'error'); }
    });
    enrollmentForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (photos.length < 3) { setMessage(messageEl, 'Capture at least three face photos first.', 'error'); return; }
      if (!uidInput.value.trim()) { setMessage(messageEl, 'Scan the student’s NFC card first.', 'error'); return; }
      const fields = new FormData(enrollmentForm);
      const payload = { roll_no: fields.get('roll_no'), name: fields.get('name'), department: fields.get('department'), year: fields.get('year'), class_name: fields.get('class_name'), uid: fields.get('uid'), photos };
      submitButton.disabled = true; submitButton.textContent = 'Validating and saving…';
      setMessage(messageEl, 'Checking the images and saving student, card and face photos together. This can take a moment.', 'info');
      try {
        const result = await api('/api/students', { method: 'POST', body: JSON.stringify(payload) });
        setMessage(messageEl, result.message, 'success');
        window.setTimeout(() => window.location.reload(), 900);
      } catch (error) {
        setMessage(messageEl, error.message, 'error'); submitButton.disabled = false; submitButton.innerHTML = 'Save student enrollment <span>→</span>';
      }
    });
    refreshPhotos();
    window.addEventListener('beforeunload', () => {
      if (scanTimer) clearTimeout(scanTimer);
      stream?.getTracks().forEach((track) => track.stop());
    });
  }

  // Lecture creation.
  const lectureForm = document.getElementById('lecture-form');
  lectureForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const fields = new FormData(lectureForm);
    const payload = Object.fromEntries(fields.entries());
    const messageEl = document.getElementById('lecture-message');
    const button = lectureForm.querySelector('button[type="submit"]');
    button.disabled = true; setMessage(messageEl, 'Saving lecture…', 'info');
    try {
      const result = await api('/api/lectures', { method: 'POST', body: JSON.stringify(payload) });
      setMessage(messageEl, `${result.message} ID: ${result.lecture_id}`, 'success');
      window.setTimeout(() => window.location.assign(`/lectures?date=${encodeURIComponent(payload.date)}&department=${encodeURIComponent(payload.department)}&year=${encodeURIComponent(payload.year)}&class_name=${encodeURIComponent(payload.class_name)}`), 600);
    } catch (error) { setMessage(messageEl, error.message, 'error'); button.disabled = false; }
  });

  // NFC + browser-camera verification kiosk. The serial hardware is owned by hardware_bridge.py.
  const startScannerButton = document.getElementById('start-scanner');
  if (startScannerButton) {
    const stopButton = document.getElementById('stop-scanner');
    const lectureSelect = document.getElementById('active-lecture');
    const video = document.getElementById('kiosk-video');
    const placeholder = document.getElementById('camera-placeholder');
    const statusPill = document.getElementById('scanner-status');
    const scanTitle = document.getElementById('scan-title');
    const scanMessage = document.getElementById('scan-message');
    const scanStudent = document.getElementById('scan-student');
    const scanResult = document.getElementById('scan-result');
    const verifyButton = document.getElementById('verify-face');
    const stateIcon = document.getElementById('scan-state-icon');
    let stream = null;
    let running = false;
    let cursor = 0;
    let timer = null;
    let queue = [];
    let current = null;
    let verifying = false;

    const setScannerStatus = (label, on) => { statusPill.innerHTML = `<span class="pulse-dot"></span> ${label}`; statusPill.className = `status-pill ${on ? 'status-live' : 'status-idle'}`; };
    const showCurrent = () => {
      if (!current) {
        scanStudent.classList.add('hidden'); verifyButton.classList.add('hidden');
        scanTitle.textContent = running ? 'Ready for an NFC scan' : 'Ready when you are';
        scanMessage.textContent = running ? 'Present one registered card. The student will appear here when the tag is received.' : 'Choose a lecture, start the kiosk and present a registered NFC card.';
        stateIcon.textContent = running ? '⌁' : '◎'; return;
      }
      if (!current.student) {
        scanStudent.classList.add('hidden'); verifyButton.classList.add('hidden');
        scanTitle.textContent = 'Unknown NFC card'; scanMessage.textContent = `UID ${current.UID} is not assigned to a registered student. Enroll it on the Students page.`;
        stateIcon.textContent = '!'; scanResult.textContent = ''; current = null; window.setTimeout(showCurrent, 3500); return;
      }
      scanTitle.textContent = 'Student card recognized'; scanMessage.textContent = 'Confirm the right lecture is selected, then verify the face in the camera.';
      scanStudent.classList.remove('hidden'); scanStudent.innerHTML = `<span class="person-avatar">${escapeHtml(current.student.NAME.slice(0, 1).toUpperCase())}</span><span><strong>${escapeHtml(current.student.NAME)}</strong><small>${escapeHtml(current.student.ROLLNO)} · ${escapeHtml(current.student.DEPARTMENT)} / ${escapeHtml(formatYear(current.student.YEAR))} / ${escapeHtml(current.student.CLASS_NAME)}</small></span>`;
      verifyButton.classList.remove('hidden'); verifyButton.disabled = false; verifyButton.textContent = 'Verify face & mark present'; stateIcon.textContent = '♙';
    };
    const processEvents = (events) => {
      for (const ev of events) {
        cursor = Math.max(cursor, ev.ID);
        if (ev.STATUS === 'pending') queue.push(ev);
        else if (ev.STATUS === 'unknown_tag') { scanTitle.textContent = 'Unknown NFC card'; scanMessage.textContent = `UID ${ev.UID} is not registered. Enroll the card before using attendance.`; stateIcon.textContent = '!'; }
      }
      if (!current && queue.length) { current = queue.shift(); showCurrent(); }
    };
    const poll = async () => {
      if (!running) return;
      try { const result = await api(`/api/scans?after_id=${cursor}`); processEvents(result.events || []); }
      catch (error) { scanResult.textContent = error.message; scanResult.className = 'scan-result error'; }
      if (running) timer = window.setTimeout(poll, 700);
    };
    startScannerButton.addEventListener('click', async () => {
      if (!lectureSelect.value) { scanResult.textContent = 'Schedule and select today’s lecture before starting the kiosk.'; scanResult.className = 'scan-result error'; return; }
      startScannerButton.disabled = true;
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 640 }, height: { ideal: 480 }, facingMode: 'user' }, audio: false });
        video.srcObject = stream; placeholder.classList.add('hidden');
        const result = await api('/api/scans/cursor'); cursor = result.cursor;
        running = true; stopButton.disabled = false; lectureSelect.disabled = true;
        setScannerStatus('Live', true); scanResult.textContent = ''; scanResult.className = 'scan-result';
        scanTitle.textContent = 'Ready for an NFC scan'; scanMessage.textContent = 'Present a registered card to the reader. The browser camera is ready.';
        startScannerButton.textContent = 'Kiosk running'; poll();
      } catch (error) { scanResult.textContent = cameraError(error); scanResult.className = 'scan-result error'; startScannerButton.disabled = false; }
    });
    stopButton.addEventListener('click', () => {
      running = false; if (timer) clearTimeout(timer);
      stream?.getTracks().forEach((track) => track.stop()); stream = null; video.srcObject = null;
      placeholder.classList.remove('hidden'); stopButton.disabled = true; startScannerButton.disabled = false; startScannerButton.textContent = 'Start kiosk'; lectureSelect.disabled = false;
      setScannerStatus('Offline', false); queue = []; current = null; verifying = false; verifyButton.classList.add('hidden'); showCurrent();
    });
    verifyButton.addEventListener('click', async () => {
      if (!current || verifying || video.readyState < 2) return;
      if (!lectureSelect.value) { scanResult.textContent = 'Select the active lecture first.'; scanResult.className = 'scan-result error'; return; }
      verifying = true; verifyButton.disabled = true; verifyButton.textContent = 'Capturing & verifying…';
      scanResult.textContent = 'Keep your face centered and still for a couple of seconds…'; scanResult.className = 'scan-result info';
      try {
        const frames = [];
        for (let i = 0; i < 3; i++) {
          const canvas = document.createElement('canvas');
          canvas.width = Math.min(video.videoWidth || 640, 640);
          canvas.height = Math.round(canvas.width * ((video.videoHeight || 480) / (video.videoWidth || 640)));
          canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
          frames.push(canvas.toDataURL('image/jpeg', 0.82));
          if (i < 2) await sleep(550);
        }
        const response = await api('/api/verify', { method: 'POST', body: JSON.stringify({ scan_id: current.ID, lecture_id: Number(lectureSelect.value), frames }) });
        scanResult.textContent = response.message; scanResult.className = 'scan-result success';
        stateIcon.textContent = response.status === 'verified' ? '✓' : '↻';
        current = null; verifying = false; window.setTimeout(() => { if (!current) showCurrent(); }, 1800);
      } catch (error) {
        scanResult.textContent = error.message; scanResult.className = 'scan-result error';
        if (/expired|already|unknown|does not belong|not assigned|only be checked/i.test(error.message)) { current = null; }
        verifying = false; verifyButton.disabled = false; verifyButton.textContent = 'Retry verification';
        if (!current) window.setTimeout(showCurrent, 1200);
      }
      if (current && !verifying) { verifyButton.disabled = false; verifyButton.textContent = 'Verify face & mark present'; }
      if (!current && running) window.setTimeout(() => { if (!current && queue.length) { current = queue.shift(); showCurrent(); } else if (!current) showCurrent(); }, 2200);
    });
    window.addEventListener('beforeunload', () => { running = false; if (timer) clearTimeout(timer); stream?.getTracks().forEach((track) => track.stop()); });
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  }
})();
