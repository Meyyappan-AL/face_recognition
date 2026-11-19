(function () {
  'use strict';

  const cfg = window.FACE_CFG || {};
  const $ = (id) => document.getElementById(id);
  const esc = (v) => (window.erp && erp.esc) ? erp.esc(v == null ? '' : v) : String(v == null ? '' : v);
  const toast = (msg, type) => { if (window.erp && erp.toast) erp.toast(msg, type); };
  const api = (url, opts) => erp.fetchJSON(url, opts);

  let activeJob = null;
  let roster = [];
  let draft = {};
  let touched = new Set();
  let stream = null;
  let streamPromise = null;
  let pollTimer = null;
  let captureBusy = false;
  let latestVideoJobId = null;
  let cameraNote = 'Camera idle.';
  let cameraNoteType = 'muted';

  function selection() {
    return {
      class: $('faceClass').value,
      date: $('faceDate').value,
      period: Number($('facePeriod').value || 0),
    };
  }

  function alertBox(msg, type) {
    const host = $('faceAlertHost');
    if (!msg) { host.innerHTML = ''; return; }
    host.innerHTML = `<div class="alert alert-${type || 'info'} py-2 mb-3">${esc(msg)}</div>`;
  }

  function setCameraNote(msg, type) {
    cameraNote = msg || 'Camera idle.';
    cameraNoteType = type || 'muted';
    const el = $('faceCameraState');
    if (!el) return;
    el.className = `small text-${cameraNoteType}`;
    el.textContent = cameraNote;
  }

  function cameraError(e) {
    const name = e && e.name;
    if (name === 'NotAllowedError' || name === 'PermissionDeniedError' || name === 'SecurityError') {
      return 'Camera access was blocked. Allow camera permission and use HTTPS or localhost, then try again.';
    }
    if (name === 'NotFoundError' || name === 'DevicesNotFoundError') return 'No camera was found on this device.';
    if (name === 'NotReadableError' || name === 'TrackStartError') return 'The camera is busy. Close other apps using it, then try again.';
    if (name === 'OverconstrainedError' || name === 'ConstraintNotSatisfiedError') return 'The camera cannot satisfy the requested resolution. Try again with the default camera.';
    return (e && e.message) || 'Could not start the camera.';
  }

  function statusBadge(status) {
    const map = {
      recording: 'danger', queued: 'secondary', processing: 'warning', ready: 'success', failed: 'danger', completed: 'primary',
    };
    return `<span class="badge bg-${map[status] || 'secondary'}">${esc(status || '-')}</span>`;
  }

  function statusForStudent(sid) {
    return draft[String(sid)] || { status: '-', source: 'manual', confidence: null, flagged: 0 };
  }

  function matchedFaces(job) {
    return (job && job.faces || []).filter((f) => f.kind === 'student' && f.student_id);
  }

  function applyFaceMatches(reset) {
    const bySid = {};
    matchedFaces(activeJob).forEach((f) => { bySid[String(f.student_id)] = f; });
    roster.forEach((s) => {
      const key = String(s.id);
      const d = draft[key] || (draft[key] = { status: s.status || '-', source: 'manual', confidence: null, flagged: 0 });
      const f = bySid[key];
      if (reset) {
        d.status = s.status || '-';
        d.source = 'manual';
        d.confidence = null;
        d.flagged = 0;
      }
      if (f) {
        d.confidence = f.match_confidence != null ? Number(f.match_confidence) : null;
        d.flagged = Number(f.flagged || 0);
        if (!touched.has(key)) {
          d.source = 'face_attendance';
          if (['Absent', 'Not Marked', '-', ''].includes(d.status)) d.status = 'Present';
        }
      } else if (!reset && !touched.has(key) && d.source === 'face_attendance') {
        d.source = 'manual';
        d.confidence = null;
        d.flagged = 0;
        d.status = s.status || '-';
      }
    });
  }

  function renderCameraState() {
    const recording = !!(activeJob && activeJob.source === 'webcam' && activeJob.status === 'recording');
    $('faceCameraStart').disabled = recording && !!stream;
    $('faceCaptureBtn').disabled = !(recording && stream) || captureBusy;
    $('faceCameraStop').disabled = !recording;
    $('webcamJobState').innerHTML = activeJob && activeJob.source === 'webcam'
      ? `${statusBadge(activeJob.status)} <span class="small text-muted">Job #${activeJob.id}</span>`
      : 'No active job';
    $('videoJobState').innerHTML = activeJob && activeJob.source === 'video'
      ? `${statusBadge(activeJob.status)} <span class="small text-muted">Job #${activeJob.id}</span>`
      : (latestVideoJobId ? `<span class="small text-muted">Last job #${latestVideoJobId}</span>` : 'No active video job');
  }

  function renderReview() {
    const job = activeJob;
    $('faceReviewCard').classList.toggle('d-none', !job);
    if (!job) return;
    $('faceJobTitle').textContent = `#${job.id} · ${job.source} · ${job.class_name} · ${job.date} P${job.period}`;
    $('faceJobStatus').outerHTML = statusBadge(job.status).replace('<span ', '<span id="faceJobStatus" ');
    const progress = Number(job.progress || 0);
    $('faceJobProgress').style.width = progress + '%';
    $('faceJobProgress').textContent = progress + '%';
    $('faceStatFrames').textContent = job.frames_scanned || 0;
    $('faceStatFaces').textContent = job.faces_detected || 0;
    $('faceStatMatched').textContent = job.matched_students || 0;
    $('faceStatUnknown').textContent = job.unknown_faces || 0;
    $('faceStatThreshold').textContent = Math.round((cfg.minConfidence || 0) * 100) + '%';

    const err = $('faceJobError');
    if (job.error) { err.textContent = job.error; err.classList.remove('d-none'); }
    else { err.textContent = ''; err.classList.add('d-none'); }

    const canSubmit = job.status === 'ready' && roster.length > 0;
    $('faceSubmitAttendance').disabled = !canSubmit;
    $('faceDeleteJob').disabled = ['queued', 'processing', 'recording'].includes(job.status);
    if (job.status === 'recording') {
      $('faceSubmitHint').textContent = 'Stop the webcam before submitting the reviewed draft.';
    } else if (job.status === 'processing' || job.status === 'queued') {
      $('faceSubmitHint').textContent = 'Video processing is running. Review will be enabled when it completes.';
    } else if (job.status === 'completed') {
      $('faceSubmitHint').textContent = 'This job was completed. Video has been deleted.';
    } else if (job.status === 'failed') {
      $('faceSubmitHint').textContent = 'This job failed. Delete it and retry with a supported video.';
    } else {
      $('faceSubmitHint').textContent = 'Recognition is a draft only. Verify each row and submit through the normal attendance validation.';
    }

    renderUnknownFaces();
    renderRoster();
    renderCameraState();
    renderVideoBox();
  }

  function renderUnknownFaces() {
    const faces = (activeJob && activeJob.faces || []).filter((f) => f.kind === 'unknown');
    const wrap = $('faceUnknownWrap');
    wrap.classList.toggle('d-none', !faces.length);
    if (!faces.length) { $('faceUnknownList').innerHTML = ''; return; }
    const used = new Set(matchedFaces(activeJob).map((f) => String(f.student_id)));
    const options = roster.filter((s) => !used.has(String(s.id)))
      .map((s) => `<option value="${s.id}">${String(s.student_role || '').toLowerCase() === 'representative' ? 'Rep ' : ''}${esc(s.name)} (${esc(s.reg_no || s.roll || s.id)})</option>`).join('');
    $('faceUnknownList').innerHTML = faces.map((f) => {
      const conf = f.match_confidence != null ? Math.round(Number(f.match_confidence) * 100) : 0;
      return `<div class="face-unknown-card border rounded p-2 bg-white">
        <img class="face-crop face-flag mb-2" src="${esc(f.crop_url || '')}" alt="Unknown face">
        <div class="small fw-semibold">Unknown face · ${f.sample_count || 1} sample(s)</div>
        <div class="small text-muted mb-2">Best model score: ${conf}%</div>
        <select class="form-select form-select-sm mb-2" id="assignSel_${f.id}">
          <option value="">Assign to student...</option>
          ${options}
        </select>
        <button class="btn btn-outline-primary btn-sm w-100 faceAssignBtn" type="button" data-face-id="${f.id}">Assign</button>
        <div class="small text-muted mt-1">Unassigned faces stay unmarked.</div>
      </div>`;
    }).join('');
  }

  function renderRoster() {
    const q = ($('faceRosterSearch').value || '').trim().toLowerCase();
    const rows = roster.filter((s) => {
      if (!q) return true;
      return String(s.name || '').toLowerCase().includes(q) || String(s.reg_no || '').toLowerCase().includes(q) || String(s.roll || '').toLowerCase().includes(q);
    });
    if (!rows.length) {
      $('faceRosterBody').innerHTML = '<tr><td colspan="5" class="text-muted small">No roster rows match.</td></tr>';
      return;
    }
    const bySid = {};
    matchedFaces(activeJob).forEach((f) => { bySid[String(f.student_id)] = f; });
    $('faceRosterBody').innerHTML = rows.map((s) => {
      const d = statusForStudent(s.id);
      const f = bySid[String(s.id)];
      const flagged = d.flagged || (f && Number(f.flagged || 0));
      const conf = d.confidence != null ? Math.round(d.confidence * 100) : null;
      const status = d.status || '-';
      const btn = (val, label, cls) => `<button type="button" class="btn btn-sm face-status-btn ${status === val ? cls : 'btn-outline-secondary'} faceStatusBtn" data-sid="${s.id}" data-status="${val}">${label}</button>`;
      const match = f
        ? `<span class="badge ${flagged ? 'bg-warning text-dark' : 'bg-success'}">${conf == null ? 'matched' : conf + '%'}</span>${flagged ? ' <span class="badge bg-warning text-dark">Review</span>' : ''}`
        : '<span class="text-muted small">-</span>';
      return `<tr class="${f ? 'face-match-row' : ''}">
        <td><div class="fw-semibold">${erp.repBadge(s.student_role)}${esc(s.name)}</div><div class="small text-muted">${esc(s.reg_no || '')}</div></td>
        <td class="small">${esc(s.roll || '')}${s.roll ? ' / ' : ''}${esc(s.reg_no || '')}</td>
        <td>${match}</td>
        <td class="text-nowrap">${btn('Present', 'P', 'btn-success')} ${btn('Absent', 'A', 'btn-danger')} ${btn('N', 'N', 'btn-warning')} ${btn('Late', 'L', 'btn-info')} ${btn('OD', 'OD', 'btn-primary')} ${btn('-', '-', 'btn-secondary')}</td>
        <td class="small">${d.source === 'face_attendance' ? '<span class="badge bg-info text-dark">AI face</span>' : '<span class="text-muted">Manual</span>'}</td>
      </tr>`;
    }).join('');
  }

  function renderVideoBox() {
    const job = activeJob;
    const videoJob = job && job.source === 'video' ? job : null;
    if (videoJob) {
      $('videoJobState').innerHTML = `${statusBadge(videoJob.status)} <span class="small text-muted">Job #${videoJob.id}</span>`;
      const p = Number(videoJob.progress || 0);
      $('videoJobProgress').style.width = p + '%';
      $('videoJobProgress').textContent = p + '%';
      $('videoJobDetails').innerHTML = `${esc(videoJob.error || videoJob.notes || 'Processing locally...')}<br>Frames: ${videoJob.frames_scanned || 0} · Faces: ${videoJob.faces_detected || 0} · Matched: ${videoJob.matched_students || 0} · Unknown: ${videoJob.unknown_faces || 0}`;
    } else if (latestVideoJobId) {
      $('videoJobState').innerHTML = `<span class="small text-muted">Last video job #${latestVideoJobId}</span>`;
    }
  }

  function renderWebcamCaptures() {
    if (!activeJob || activeJob.source !== 'webcam') {
      $('webcamCaptures').textContent = 'Captures will appear here.';
      return;
    }
    const faces = activeJob.faces || [];
    const known = faces.filter((f) => f.kind === 'student');
    const unknown = faces.filter((f) => f.kind === 'unknown');
    $('webcamCaptures').innerHTML = `<div class="mb-1">${known.length} matched student(s), ${unknown.length} unknown face group(s).</div>` +
      (known.length ? known.map((f) => `<div class="small">✓ ${erp.repBadge(f.student_role)}${esc(f.student_name || 'Student')} <span class="text-muted">${Math.round(Number(f.match_confidence || 0) * 100)}%</span></div>`).join('') : '<div class="small text-muted">No captures yet.</div>');
  }

  function setActiveJob(job, reset) {
    if (!job) return;
    const changed = !activeJob || activeJob.id !== job.id;
    activeJob = job;
    if (job.source === 'video') latestVideoJobId = job.id;
    if (changed || reset) touched = new Set();
    applyFaceMatches(!!reset || changed);
    renderReview();
    renderWebcamCaptures();
  }

  async function loadCellInfo() {
    const sel = selection();
    $('faceCellInfo').textContent = 'Checking schedule...';
    try {
      const d = await api(`/api/timetable/cell?class=${encodeURIComponent(sel.class)}&date=${encodeURIComponent(sel.date)}&period=${encodeURIComponent(sel.period)}`);
      const cell = d.cell || {};
      const label = cell.display || cell.subject || 'No scheduled class';
      $('faceCellInfo').innerHTML = d.ok
        ? `<strong>${esc(label)}</strong><br><span class="text-muted">${esc((cell.staff || []).join(', ') || 'Staff not listed')} ${cell.time ? '· ' + esc(cell.time) : ''}</span>`
        : `<span class="text-danger">${esc(label)}</span>`;
    } catch (e) {
      $('faceCellInfo').innerHTML = `<span class="text-danger">${esc(e.message)}</span>`;
    }
  }

  async function loadStatus() {
    try {
      const d = await api('/api/admin/face/status');
      const b = $('faceModelBadge');
      if (d.model_ready) {
        b.className = 'badge bg-success';
        b.textContent = 'Trained model ready';
        if (d.models) {
          const detector = d.models.detector === 'yunet' ? 'YuNet' : 'Haar';
          const embedding = d.models.embedding === 'sface' ? 'SFace' : 'Grayscale';
          b.textContent += ` · ${detector} · ${embedding}`;
          b.title = `Detector: ${detector}; recognizer: ${embedding}`;
          if (d.models.embedding === 'sface' && !d.models.sface_available) {
            b.className = 'badge bg-warning text-dark';
            b.title = 'SFace embedding metadata is saved, but the SFace model asset is missing.';
          }
        }
      } else {
        b.className = 'badge bg-warning text-dark';
        b.textContent = 'No trained model';
      }
      if (d.train && d.train.running) b.textContent += ' · training';
    } catch (e) {
      $('faceModelBadge').className = 'badge bg-danger';
      $('faceModelBadge').textContent = 'Status unavailable';
    }
  }

  function renderHistory(jobs) {
    const body = $('faceHistoryBody');
    if (!jobs || !jobs.length) {
      body.innerHTML = '<tr><td colspan="9" class="text-muted small">No face attendance jobs yet.</td></tr>';
      return;
    }
    body.innerHTML = jobs.map((j) => `<tr>
      <td class="fw-semibold">#${j.id}</td>
      <td>${esc(j.source)}</td>
      <td>${esc(j.class_name)}</td>
      <td>${esc(j.date)} / P${j.period}</td>
      <td>${statusBadge(j.status)}</td>
      <td>${j.matched_students || 0}</td>
      <td>${j.unknown_faces || 0}</td>
      <td class="small text-muted">${esc((j.created_at || '').replace('T', ' ').slice(0, 16))}</td>
      <td class="text-nowrap">
        <button class="btn btn-outline-primary btn-sm faceOpenJob" type="button" data-job-id="${j.id}">Open</button>
        <button class="btn btn-outline-danger btn-sm faceDelJob" type="button" data-job-id="${j.id}" ${['queued', 'processing', 'recording'].includes(j.status) ? 'disabled' : ''}>Delete</button>
      </td>
    </tr>`).join('');
  }

  async function loadJobs() {
    try {
      const d = await api('/api/admin/face/jobs');
      renderHistory(d.jobs || []);
    } catch (e) {
      $('faceHistoryBody').innerHTML = `<tr><td colspan="9" class="text-danger small">${esc(e.message)}</td></tr>`;
    }
  }

  async function loadRosterForJob(job) {
    const d = await api(`/api/attendance/roster?class=${encodeURIComponent(job.class_name)}&date=${encodeURIComponent(job.date)}&period=${encodeURIComponent(job.period)}`);
    roster = d.students || [];
    draft = {};
    touched = new Set();
    roster.forEach((s) => { draft[String(s.id)] = { status: s.status || '-', source: 'manual', confidence: null, flagged: 0 }; });
  }

  async function openJob(jobId) {
    try {
      alertBox('');
      const d = await api(`/api/admin/face/jobs/${jobId}`);
      const job = d.job;
      if (job.source === 'video') latestVideoJobId = job.id;
      if (stream && (job.source !== 'webcam' || job.status !== 'recording')) stopStream();
      await loadRosterForJob(job);
      setActiveJob(job, true);
      $('faceClass').value = job.class_name;
      $('faceDate').value = job.date;
      $('facePeriod').value = job.period;
      await loadCellInfo();
      $('faceReviewCard').scrollIntoView({ behavior: 'smooth', block: 'start' });
      if (['queued', 'processing'].includes(job.status)) pollJob(job.id);
      renderCameraState();
    } catch (e) {
      alertBox(e.message, 'danger');
    }
  }

  async function refreshJob(jobId, keepReset) {
    const d = await api(`/api/admin/face/jobs/${jobId}`);
    if (stream && (d.job.source !== 'webcam' || d.job.status !== 'recording')) stopStream();
    if (!activeJob || activeJob.id === jobId) setActiveJob(d.job, !!keepReset);
    return d.job;
  }

  function schedulePoll(job) {
    clearTimeout(pollTimer);
    if (!job || !['queued', 'processing'].includes(job.status)) return;
    pollTimer = setTimeout(async () => {
      try {
        const latest = await refreshJob(job.id, false);
        schedulePoll(latest);
        if (latest.status === 'ready') {
          toast('Video processing complete. Review the draft.');
          loadJobs();
          loadStatus();
        }
        if (latest.status === 'failed') {
          alertBox(latest.error || 'Video processing failed.', 'danger');
          loadJobs();
        }
      } catch (e) {
        toast(e.message, 'danger');
      }
    }, 2200);
  }

  function pollJob(jobId) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      try {
        const latest = await refreshJob(jobId, false);
        schedulePoll(latest);
      } catch (e) {
        toast(e.message, 'danger');
      }
    }, 800);
  }

  function handleTrackEnded() {
    if (!stream) return;
    stopStream();
    setCameraNote('Camera disconnected. Start Webcam Job again to reconnect.', 'warning');
  }

  function waitForVideoFrame(video) {
    if (video.readyState >= 2 && video.videoWidth && video.videoHeight) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { cleanup(); reject(new Error('Camera did not provide a frame within 8 seconds.')); }, 8000);
      const success = () => {
        if (video.videoWidth && video.videoHeight) { cleanup(); resolve(); }
      };
      const failure = () => { cleanup(); reject(new Error('Camera preview failed.')); };
      function cleanup() {
        clearTimeout(timer);
        video.removeEventListener('loadedmetadata', success);
        video.removeEventListener('canplay', success);
        video.removeEventListener('playing', success);
        video.removeEventListener('error', failure);
      }
      video.addEventListener('loadedmetadata', success);
      video.addEventListener('canplay', success);
      video.addEventListener('playing', success);
      video.addEventListener('error', failure);
    });
  }

  async function startStream() {
    if (stream) return stream;
    if (streamPromise) return streamPromise;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error('Camera access requires HTTPS or localhost and a supported browser.');
    }
    setCameraNote('Requesting camera access...', 'info');
    streamPromise = (async () => {
      let media;
      try {
        media = await navigator.mediaDevices.getUserMedia({
          video: { width: { ideal: 960 }, height: { ideal: 720 }, facingMode: 'user' },
          audio: false,
        });
      } catch (e) {
        if (e && (e.name === 'OverconstrainedError' || e.name === 'ConstraintNotSatisfiedError')) {
          media = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
        } else {
          throw e;
        }
      }
      stream = media;
      const video = $('faceVideo');
      video.srcObject = stream;
      stream.getVideoTracks().forEach((track) => track.addEventListener('ended', handleTrackEnded));
      try {
        await video.play();
      } catch (e) {
        if (!e || e.name !== 'AbortError') throw new Error('Camera preview could not start.');
      }
      await waitForVideoFrame(video);
      setCameraNote('Camera ready. Keep one student in frame.', 'success');
      return stream;
    })();
    try {
      return await streamPromise;
    } catch (e) {
      stopStream();
      const message = cameraError(e);
      setCameraNote(message, 'danger');
      throw new Error(message);
    } finally {
      streamPromise = null;
    }
  }

  function stopStream() {
    if (stream) {
      stream.getVideoTracks().forEach((track) => track.removeEventListener('ended', handleTrackEnded));
      stream.getTracks().forEach((track) => track.stop());
      stream = null;
    }
    const video = $('faceVideo');
    if (video) video.srcObject = null;
    setCameraNote('Camera stopped.', 'muted');
    renderCameraState();
  }

  async function startWebcamJob() {
    try {
      alertBox('');
      if (activeJob && activeJob.status === 'recording') {
        await startStream();
        renderCameraState();
        return;
      }
      const sel = selection();
      await startStream();
      const d = await api('/api/admin/face/webcam/start', { method: 'POST', body: sel });
      const job = d.job;
      await loadRosterForJob(job);
      setActiveJob(job, true);
      $('webcamCaptures').innerHTML = '<div class="small text-muted">Camera ready. Capture one student at a time.</div>';
      toast('Webcam job started. Capture each student, then stop and review.');
      renderCameraState();
      loadJobs();
    } catch (e) {
      stopStream();
      alertBox(e.message, 'danger');
      toast(e.message, 'danger');
    }
  }

  async function captureFace() {
    if (!activeJob || activeJob.status !== 'recording' || captureBusy) return;
    captureBusy = true;
    $('faceCaptureBtn').disabled = true;
    $('faceCaptureBtn').textContent = 'Capturing...';
    try {
      if (!stream) await startStream();
      const video = $('faceVideo');
      if (!video.videoWidth || !video.videoHeight) throw new Error('Camera frame is not ready.');
      const canvas = document.createElement('canvas');
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.9));
      if (!blob) throw new Error('Could not capture the webcam frame.');
      const fd = new FormData();
      fd.append('image', blob, 'webcam.jpg');
      const d = await api(`/api/admin/face/jobs/${activeJob.id}/frame`, { method: 'POST', body: fd });
      setActiveJob(d.job, false);
      renderWebcamCaptures();
      if (d.detected) toast('Face captured and added to the draft.');
      else alertBox(d.warning || 'No face detected.', 'warning');
      loadJobs();
    } catch (e) {
      alertBox(e.message, 'danger');
      toast(e.message, 'danger');
    } finally {
      captureBusy = false;
      $('faceCaptureBtn').textContent = 'Capture Face';
      renderCameraState();
    }
  }

  async function stopWebcamJob() {
    if (!activeJob || activeJob.source !== 'webcam') { stopStream(); return; }
    try {
      const d = await api(`/api/admin/face/jobs/${activeJob.id}/stop`, { method: 'POST', body: {} });
      stopStream();
      setActiveJob(d.job, false);
      toast('Webcam stopped. Review the draft below.');
      loadJobs();
    } catch (e) {
      alertBox(e.message, 'danger');
    }
  }

  function uploadVideo() {
    const sel = selection();
    const file = $('faceVideoFile').files && $('faceVideoFile').files[0];
    if (!file) { alertBox('Choose a classroom video first.', 'warning'); return; }
    if (activeJob && activeJob.status === 'recording') {
      alertBox('Stop the webcam job before uploading a video.', 'warning');
      return;
    }
    const fd = new FormData();
    fd.append('class', sel.class);
    fd.append('date', sel.date);
    fd.append('period', String(sel.period));
    fd.append('video', file, file.name);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/admin/face/jobs');
    xhr.upload.onprogress = (ev) => {
      if (!ev.lengthComputable) return;
      const pct = Math.max(1, Math.min(99, Math.round(ev.loaded / ev.total * 100)));
      $('faceUploadProgress').style.width = pct + '%';
      $('faceUploadProgress').textContent = pct + '%';
      $('faceVideoHint').textContent = `Uploading ${pct}%...`;
    };
    xhr.onload = async () => {
      let d = {};
      try { d = JSON.parse(xhr.responseText || '{}'); } catch (_) { d = {}; }
      if (xhr.status >= 200 && xhr.status < 300 && d.ok) {
        $('faceUploadProgress').style.width = '100%';
        $('faceUploadProgress').textContent = 'Uploaded';
        $('faceVideoHint').textContent = `Job #${d.job.id} queued for local processing.`;
        latestVideoJobId = d.job.id;
        try {
          await loadRosterForJob(d.job);
          setActiveJob(d.job, true);
          schedulePoll(d.job);
          loadJobs();
          loadStatus();
        } catch (e) {
          alertBox(e.message, 'danger');
        }
      } else {
        $('faceUploadProgress').style.width = '0%';
        $('faceUploadProgress').textContent = '0%';
        const msg = d.error || `Upload failed (HTTP ${xhr.status}).`;
        alertBox(msg, 'danger');
        $('faceVideoHint').textContent = msg;
      }
    };
    xhr.onerror = () => {
      $('faceVideoHint').textContent = 'Network error during upload.';
      alertBox('Network error during upload.', 'danger');
    };
    xhr.send(fd);
  }

  async function submitDraft() {
    if (!activeJob || !roster.length) return;
    if (activeJob.status !== 'ready') { alertBox('Stop capture or wait for processing before submitting.', 'warning'); return; }
    const btn = $('faceSubmitAttendance');
    btn.disabled = true;
    btn.textContent = 'Submitting...';
    try {
      const marks = roster.map((s) => {
        const d = statusForStudent(s.id);
        const ai = d.source === 'face_attendance';
        return {
          student_id: s.id,
          status: d.status || '-',
          source: ai ? 'face_attendance' : 'manual',
          confidence: ai ? d.confidence : null,
          flagged: ai ? (d.flagged ? 1 : 0) : 0,
        };
      });
      const body = {
        class: activeJob.class_name,
        date: activeJob.date,
        period: activeJob.period,
        marks,
        source: 'face_attendance',
        notes: `Face attendance job #${activeJob.id} (${activeJob.source}) reviewed by admin.`,
      };
      const res = await api('/api/attendance/submit', { method: 'POST', body });
      try {
        const done = await api(`/api/admin/face/jobs/${activeJob.id}/complete`, { method: 'POST', body: {} });
        setActiveJob(done.job, false);
        toast('Attendance submitted and face attendance job completed.');
      } catch (e) {
        toast('Attendance submitted, but the job could not be marked complete: ' + e.message, 'warning');
      }
      alertBox(`Attendance submitted for ${activeJob.class_name} ${activeJob.date} P${activeJob.period}.` +
        (res.blocked ? ` ${res.blocked} row(s) were blocked by OD/medical locks.` : ''), 'success');
      stopStream();
      loadJobs();
      loadStatus();
    } catch (e) {
      alertBox(e.message, 'danger');
      toast(e.message, 'danger');
    } finally {
      btn.textContent = 'Submit Attendance Draft';
      renderReview();
    }
  }

  async function assignFace(faceId) {
    const sel = $('assignSel_' + faceId);
    const studentId = sel ? Number(sel.value) : 0;
    if (!studentId) { alertBox('Choose a student for this face.', 'warning'); return; }
    try {
      const d = await api(`/api/admin/face/jobs/${activeJob.id}/assign`, { method: 'POST', body: { face_id: faceId, student_id: studentId } });
      touched.delete(String(studentId));
      setActiveJob(d.job, false);
      toast('Unknown face assigned. Verify the draft row before submitting.');
      loadJobs();
    } catch (e) {
      alertBox(e.message, 'danger');
    }
  }

  async function deleteJob(jobId) {
    if (!confirm('Delete this face attendance job, its derived crops and any uploaded video?')) return;
    try {
      await api(`/api/admin/face/jobs/${jobId}`, { method: 'DELETE' });
      if (activeJob && activeJob.id === jobId) {
        activeJob = null; roster = []; draft = {}; touched = new Set();
        $('faceReviewCard').classList.add('d-none');
        stopStream();
      }
      toast('Face attendance job deleted.');
      loadJobs();
    } catch (e) {
      alertBox(e.message, 'danger');
    }
  }

  function bindEvents() {
    $('faceRefreshBtn').addEventListener('click', () => { loadStatus(); loadJobs(); loadCellInfo(); });
    $('faceClass').addEventListener('change', loadCellInfo);
    $('faceDate').addEventListener('change', loadCellInfo);
    $('facePeriod').addEventListener('change', loadCellInfo);
    $('faceCameraStart').addEventListener('click', startWebcamJob);
    $('faceCaptureBtn').addEventListener('click', captureFace);
    $('faceCameraStop').addEventListener('click', stopWebcamJob);
    $('faceVideoUpload').addEventListener('click', uploadVideo);
    $('faceSubmitAttendance').addEventListener('click', submitDraft);
    $('faceDeleteJob').addEventListener('click', () => { if (activeJob) deleteJob(activeJob.id); });
    $('faceRosterSearch').addEventListener('input', renderRoster);
    $('faceAllAbsent').addEventListener('click', () => {
      roster.forEach((s) => {
        const key = String(s.id);
        draft[key] = Object.assign({}, statusForStudent(s.id), { status: 'N', source: 'manual', confidence: null, flagged: 0 });
        touched.add(key);
      });
      renderRoster();
    });
    $('faceRosterBody').addEventListener('click', (ev) => {
      const btn = ev.target.closest('.faceStatusBtn');
      if (!btn) return;
      const sid = btn.dataset.sid;
      const d = statusForStudent(sid);
      d.status = btn.dataset.status;
      d.source = 'manual';
      d.confidence = null;
      d.flagged = 0;
      touched.add(String(sid));
      renderRoster();
    });
    $('faceUnknownList').addEventListener('click', (ev) => {
      const btn = ev.target.closest('.faceAssignBtn');
      if (btn) assignFace(Number(btn.dataset.faceId));
    });
    $('faceHistoryBody').addEventListener('click', (ev) => {
      const open = ev.target.closest('.faceOpenJob');
      const del = ev.target.closest('.faceDelJob');
      if (open) openJob(Number(open.dataset.jobId));
      if (del) deleteJob(Number(del.dataset.jobId));
    });
    window.addEventListener('beforeunload', stopStream);
    document.addEventListener('visibilitychange', () => {
      if (document.hidden && stream) {
        stopStream();
        setCameraNote('Camera paused while the tab was hidden. Reconnect to capture.', 'warning');
      }
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    bindEvents();
    $('faceDate').value = window.FACE_TODAY || $('faceDate').value;
    loadStatus();
    loadJobs();
    loadCellInfo();
    setInterval(() => {
      const hist = $('historyTab');
      if (hist && hist.classList.contains('active')) loadJobs();
    }, 8000);
  });
})();
