import io
import os

import pymupdf as fitz
from PIL import Image


def pdf_to_image(pdf_path, output_dir, dpi=300):
    """
    Convertit la première page d'un PDF en image (mono ou multi-pages).

    Args:
        pdf_path (str): Chemin vers le fichier PDF
        output_dir (str): Répertoire de sortie pour l'image
        dpi (int): Résolution de l'image (défaut: 300 DPI)

    Returns:
        str: Chemin vers l'image générée
    """
    try:
        pdf_document = fitz.open(pdf_path)

        page = pdf_document[0]
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat)
        img = Image.open(io.BytesIO(pix.tobytes("png")))

        image_filename = os.path.splitext(os.path.basename(pdf_path))[0] + ".png"
        image_path = os.path.join(output_dir, image_filename)
        img.save(image_path, "PNG", optimize=True)

        pdf_document.close()

        return image_path

    except Exception as e:
        raise Exception(f"Erreur lors de la conversion PDF vers image: {str(e)}")
