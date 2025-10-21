// static/js/camera_signout.js - Manages the one-time camera capture for sign out

const signOutBtn = document.getElementById("signOutBtn");
const signOutStatus = document.getElementById("signOutStatus");
const signOutVideo = document.getElementById("signOutVideo");

let signOutStream = null;

async function startSignOutCamera() {
    signOutBtn.disabled = true;
    signOutStatus.className = "alert alert-info mt-2 small";
    signOutStatus.textContent = "Starting camera...";
    signOutVideo.style.display = 'block'; // Ensure video is visible

    try {
        signOutStream = await navigator.mediaDevices.getUserMedia({ video: { width: 320, height: 240 } });
        signOutVideo.srcObject = signOutStream;
        await signOutVideo.play();

        signOutStatus.textContent = "Camera active. Hold still. Capturing in 2 seconds...";
        setTimeout(captureAndSignOut, 2000);

    } catch (err) {
        signOutStatus.className = "alert alert-danger mt-2 small";
        signOutStatus.textContent = "Camera access denied or failed.";
        console.error("Camera access error:", err);
        signOutBtn.disabled = false;
        signOutVideo.style.display = 'none'; // Hide video on error
    }
}

async function captureAndSignOut() {
    const canvas = document.createElement("canvas");
    canvas.width = signOutVideo.videoWidth || 320;
    canvas.height = signOutVideo.videoHeight || 240;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(signOutVideo, 0, 0, canvas.width, canvas.height);

    // Stop the camera immediately after capture
    if (signOutStream) signOutStream.getTracks().forEach(t => t.stop());
    signOutVideo.srcObject = null;
    signOutVideo.style.display = 'none'; // Hide video after capture

    const blob = await new Promise(r => canvas.toBlob(r, "image/jpeg", 0.9));
    signOutStatus.textContent = "Processing face for sign-out...";

    const fd = new FormData();
    fd.append("image", blob, "signout_snap.jpg");

    try {
        const res = await fetch("/sign_out_face", { method: "POST", body: fd });
        const j = await res.json(); // Assume server always returns JSON

        if (j.status === "signed_out") {
            signOutStatus.className = "alert alert-success mt-2 small";
            signOutStatus.innerHTML = `✅ **Signed Out Successful!** Goodbye, ${j.name}.`;
        } else if (j.status === "low_conf") {
            signOutStatus.className = "alert alert-warning mt-2 small";
            signOutStatus.textContent = `❌ Sign Out Failed: Face not recognized clearly enough. Try again.`;
        } else if (j.status === "no_face") {
             signOutStatus.className = "alert alert-warning mt-2 small";
             signOutStatus.textContent = `❌ Sign Out Failed: No face detected in the picture. Try again.`;
        } else if (j.status === "no_model") {
            signOutStatus.className = "alert alert-danger mt-2 small";
            signOutStatus.textContent = `❌ Sign Out Failed: Recognition model not available. Contact Admin.`;
        } else { // Handle generic errors or unexpected statuses
            signOutStatus.className = "alert alert-danger mt-2 small";
            signOutStatus.textContent = `Error during sign-out: ${j.error || j.status || "Unknown issue."}`;
        }
    } catch (err) {
        signOutStatus.className = "alert alert-danger mt-2 small";
        signOutStatus.textContent = "Network error during sign-out. Please try again.";
        console.error('Sign Out Network Error:', err);
    } finally {
        signOutBtn.disabled = false; // Re-enable button regardless of outcome
    }
}

if(signOutBtn) { // Check if the button exists on the current page
    signOutBtn.addEventListener("click", startSignOutCamera);
    // Initialize status
    document.addEventListener("DOMContentLoaded", () => {
        signOutStatus.textContent = "Click the button to use face recognition for sign-out.";
        signOutVideo.style.display = 'none'; // Keep video hidden initially
    });
} else {
    console.log("Sign out button not found on this page.");
}
