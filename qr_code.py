import qrcode
import argparse
from datetime import datetime

def generate_qr_code(link, file_name="qr_code.png"):
    # Create a QR code instance
    qr = qrcode.QRCode(
        version=1,  # Controls the size of the QR code
        error_correction=qrcode.constants.ERROR_CORRECT_L,  # About 7% error correction
        box_size=10,  # Size of each box in pixels
        border=4,  # Border size (minimum 4)
    )
    
    # Add the link to the QR code
    qr.add_data(link)
    qr.make(fit=True)
    
    # Create an image from the QR code
    img = qr.make_image(fill='black', back_color='white')
    
    # Save the QR code as an image file
    img.save(file_name)
    print(f"QR code saved as {file_name}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--link',
        '-l',
        required="True",
        type=str,
    )
    parser.add_argument(
        '--filename',
        '-f',
        type=str,
        default=f"qr_code_{datetime.now().strftime("%Y%m%d%H%M%S")}.png"
    )
    args = parser.parse_args()

    generate_qr_code(args.link, args.filename)
