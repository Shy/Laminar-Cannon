from flask import Flask, request, jsonify
from temporalio.client import Client
import asyncio
import base64

app = Flask(__name__)


@app.route("/detect-person", methods=["POST"])
def detect_person():
    """
    POST endpoint that accepts an image file and returns whether a person is
    detected.
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

        return jsonify({"person_detected": result})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


async def execute_person_detection_workflow(image_b64: str) -> bool:
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
    app.run(debug=True, host="0.0.0.0", port=5000)
