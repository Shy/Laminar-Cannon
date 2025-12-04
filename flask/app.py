"""
Laminar Cannon - Flask Detection Server

Flask server that receives images from the Pi and runs face detection
via Temporal workflows. Returns servo movement commands to track detected faces.
"""

from flask import Flask, request, jsonify
from temporalio.client import Client
import asyncio
import base64

app = Flask(__name__)


@app.route("/detect-person", methods=["POST"])
def detect_person():
    """
    Face detection endpoint.

    Accepts an image file, runs YOLO detection via Temporal workflow,
    returns whether a person is detected and servo movement commands.

    Returns:
        JSON with person_detected, servo_control, and detection_info
    """
    try:
        # Check if image is provided
        if "image" not in request.files:
            return jsonify({"error": "No image provided"}), 400

        image_file = request.files["image"]
        if image_file.filename == "":
            return jsonify({"error": "No image selected"}), 400

        # Read image data
        image_data = image_file.read()

        # Convert to base64 for Temporal workflow
        image_b64 = base64.b64encode(image_data).decode("utf-8")

        # Run Temporal workflow
        result = asyncio.run(execute_person_detection_workflow(image_b64))

        # Prepare response
        if result is None:
            return jsonify({
                "person_detected": False,
                "servo_control": None,
                "detection_info": None
            })
        else:
            return jsonify({
                "person_detected": True,
                "servo_control": {
                    "x_steps": result["servo_control"]["x_steps"],
                    "y_steps": result["servo_control"]["y_steps"]
                },
                "detection_info": {
                    "confidence": result["confidence"],
                    "location": result["box_info"],
                    "offset_info": {
                        "pixels": result["servo_control"]["offset_pixels"],
                        "degrees": result["servo_control"]["offset_degrees"]
                    }
                }
            })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


async def execute_person_detection_workflow(image_b64: str):
    """
    Execute the Temporal workflow for person detection.
    """
    client = await Client.connect("localhost:7233")

    result = await client.execute_workflow(
        "personDetection",
        image_b64,
        id=f"person-detection-{hash(image_b64)}",
        task_queue="person-detection-task-queue",
    )

    return result


@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    return jsonify({"status": "healthy"})


if __name__ == "__main__":
    import os
    port = int(os.getenv("FLASK_PORT", 5001))
    app.run(debug=True, host="0.0.0.0", port=port)
