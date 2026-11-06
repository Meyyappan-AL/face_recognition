// static/js/camera_add_student.js
const video = document.getElementById('video');
const startCaptureBtn = document.getElementById('startCaptureBtn');
const progressBar = document.getElementById('progressBar');
const captureStatus = document.getElementById('captureStatus');
const addStudentBtn = document.getElementById('addStudentBtn');
const saveInfoBtn = document.getElementById('saveInfoBtn');
const studentForm = document.getElementById('studentForm');

let stream;
let captureInterval;
let capturedImages = [];
const CAPTURE_COUNT = 50;
let currentStudentId = null;

// --- 1. Start camera feed automatically on page load ---
async function startCamera() {
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: { width: 560, height: 420 } });
    video.srcObject = stream;
    await video.play();
  } catch (err) {
    alert("Camera access error: " + err.message);
    captureStatus.textContent = "Camera could not be started.";
  }
}

// --- 2. Save Student Info First ---
studentForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  saveInfoBtn.disabled = true;
  saveInfoBtn.textContent = "Saving...";
  const formData = new FormData(studentForm);

  try {
    const response = await fetch('/add_student', { method: 'POST', body: formData });
    const data = await response.json();
    if (response.ok) {
      currentStudentId = data.student_id;
      alert(`Student info saved! ID: ${currentStudentId}. You can now capture face images.`);
      startCaptureBtn.disabled = false; // Enable the capture button
      saveInfoBtn.classList.remove('btn-primary');
      saveInfoBtn.classList.add('btn-secondary');
      saveInfoBtn.textContent = "Info Saved";
    } else {
      alert(`Error: ${data.error || 'Unknown error'}`);
      saveInfoBtn.disabled = false;
      saveInfoBtn.textContent = "1. Save Info";
    }
  } catch (err) {
    alert('Network error while saving student info.');
    saveInfoBtn.disabled = false;
    saveInfoBtn.textContent = "1. Save Info";
  }
});

// --- 3. Start Capturing a Sequence of Face Images ---
startCaptureBtn.addEventListener('click', () => {
  if (!currentStudentId) {
    alert("Please save the student's information first.");
    return;
  }
  capturedImages = [];
  progressBar.style.width = '0%';
  captureStatus.textContent = `Starting capture...`;
  startCaptureBtn.disabled = true;
  addStudentBtn.disabled = true;
  let count = 0;

  captureInterval = setInterval(async () => {
    if (count >= CAPTURE_COUNT) {
      clearInterval(captureInterval);
      captureStatus.textContent = `Capture complete. Ready to add student.`;
      startCaptureBtn.textContent = "2. Re-Capture (50)";
      startCaptureBtn.disabled = false; // Allow re-capture
      addStudentBtn.disabled = false;   // Enable the final button
      return;
    }

    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext('2d').drawImage(video, 0, 0);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.9));

    if (blob) {
      capturedImages.push(blob);
      count++;
      const progress = Math.round((count / CAPTURE_COUNT) * 100);
      progressBar.style.width = progress + '%';
      captureStatus.textContent = `Captured ${count} / ${CAPTURE_COUNT}`;
    }
  }, 200); // Capture a frame every 200ms
});


// --- 4. Finalize and Upload Everything ---
addStudentBtn.addEventListener('click', async () => {
  if (capturedImages.length < CAPTURE_COUNT) {
    alert('Please complete the face capture sequence first.');
    return;
  }
  addStudentBtn.disabled = true;
  addStudentBtn.textContent = "Uploading...";
  startCaptureBtn.disabled = true;

  const uploadData = new FormData();
  uploadData.append('student_id', currentStudentId);
  capturedImages.forEach((blob, index) => {
    uploadData.append('images[]', blob, `capture_${index}.jpg`);
  });

  try {
    const response = await fetch('/upload_face', { method: 'POST', body: uploadData });
    const data = await response.json();
    if (response.ok) {
      alert(`Successfully added student and uploaded ${data.saved} faces! You should retrain the model now.`);
      window.location.href = '/'; // Redirect to admin dashboard
    } else {
      alert(`Error uploading faces: ${data.error || 'Unknown error'}`);
      addStudentBtn.disabled = false;
      addStudentBtn.textContent = "3. Finish & Add Student";
      startCaptureBtn.disabled = false;
    }
  } catch (err) {
    alert('A network error occurred during the upload.');
    addStudentBtn.disabled = false;
    addStudentBtn.textContent = "3. Finish & Add Student";
    startCaptureBtn.disabled = false;
  }
});

// Initialize by starting the camera
startCamera();
