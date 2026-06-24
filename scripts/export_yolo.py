from ultralytics import YOLO
import os

def convert_to_engine(model_path, imgsz=(736, 1280)):
    """
    Converts a YOLO PyTorch (.pt) model to TensorRT (.engine) format.
    
    Args:
        model_path (str): Path to your .pt file.
        imgsz (tuple): The exact resolution you use in your detector.
    """
    print(f"Loading model: {model_path}")
    model = YOLO(model_path)

    # Exporting the model
    # half=True:  Enables FP16. Essential for modern NVIDIA GPUs.
    # device=0:   Forces the export to happen on the GPU.
    # workspace=4: Limits GPU memory usage during conversion (in GB).
    print("Starting export to TensorRT... (this may take several minutes)")
    
    export_path = model.export(
        format='engine', 
        imgsz=imgsz, 
        half=True, 
        device=0, 
        simplify=True,
        workspace=4  # Adjust based on your GPU VRAM
    )
    
    print(f"Export successful! Engine saved at: {export_path}")
    return export_path

if __name__ == "__main__":
    # Path to your existing .pt model
    MY_MODEL = "checkpoints/detection_1280_720_yolo11m.pt"
    
    if os.path.exists(MY_MODEL):
        convert_to_engine(MY_MODEL)
    else:
        print(f"Error: {MY_MODEL} not found.")