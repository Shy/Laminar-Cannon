#!/usr/bin/env python3
"""
Simple test script for the person detection system.
This demonstrates how to test the Flask endpoint.
"""

import requests
import sys
import os


def test_health_endpoint():
    """Test the health check endpoint"""
    try:
        response = requests.get("http://localhost:5000/health")
        if response.status_code == 200:
            print("✅ Health endpoint working")
            return True
        else:
            print(f"❌ Health endpoint failed: {response.status_code}")
            return False
    except requests.exceptions.ConnectionError:
        print("❌ Cannot connect to Flask server. Make sure it's running on port 5000")
        return False


def test_person_detection(image_path):
    """Test person detection endpoint with an image"""
    if not os.path.exists(image_path):
        print(f"❌ Image file not found: {image_path}")
        return False

    try:
        with open(image_path, "rb") as f:
            files = {"image": f}
            response = requests.post("http://localhost:5000/detect-person", files=files)

        if response.status_code == 200:
            result = response.json()
            person_detected = result.get("person_detected", False)
            print(f"✅ Person detection result: {person_detected}")
            return True
        else:
            print(f"❌ Person detection failed: {response.status_code}")
            print(f"Response: {response.text}")
            return False
    except requests.exceptions.ConnectionError:
        print("❌ Cannot connect to Flask server. Make sure it's running on port 5000")
        return False
    except Exception as e:
        print(f"❌ Error testing person detection: {e}")
        return False


if __name__ == "__main__":
    print("🧪 Testing Person Detection System")
    print("=" * 40)

    # Test health endpoint
    if not test_health_endpoint():
        sys.exit(1)

    # Test person detection if image provided
    if len(sys.argv) > 1:
        image_path = sys.argv[1]
        print(f"\n📷 Testing person detection with image: {image_path}")
        test_person_detection(image_path)
    else:
        print("\n💡 To test person detection, run:")
        print("   python test_system.py path/to/your/image.jpg")

    print("\n🎉 Basic tests completed!")
