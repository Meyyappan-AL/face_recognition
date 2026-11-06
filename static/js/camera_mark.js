// --- Inside js/camera_mark.js, where a face is successfully recognized ---

// Assuming 'recognizedId' and 'recognizedName' are variables holding the student data
fetch('/api/mark_attendance', {
    method: 'POST',
    headers: {
        'Content-Type': 'application/json',
    },
    body: JSON.stringify({
        student_id: recognizedId,
        name: recognizedName
    })
})
.then(response => response.json())
.then(data => {
    // Check the response from Python
    if (data.status === 'marked') {
        // Attendance successfully marked
        document.getElementById('markStatus').textContent = `✅ Attendance Recorded for ${recognizedName}`;
    } else if (data.status === 'rejected') {
        // Attendance rejected due to cool-down
        document.getElementById('markStatus').textContent = `⚠️ Already marked within cooldown.`;
    }
})
// ... rest of the JS logic