# PaddleOCR image-analysis Lambda (container image).
#
# paddlepaddle + paddleocr + opencv are far larger than the 250 MB zip limit,
# so this ships as a container image (Lambda allows up to 10 GB). The handler
# is ocr/aws_lambda_handler.py, which downloads the Slack image and runs the
# engine named by OCR_ENGINE (set to "paddleocr" below).

FROM public.ecr.aws/lambda/python:3.11

# Runtime libs (opencv needs libGL/glib) + build tools and image codec headers
# for the few packages that fall back to a source build (e.g. rapidfuzz C++,
# pillow codecs) on this base image.
RUN yum install -y \
        mesa-libGL glib2 \
        gcc gcc-c++ make \
        libjpeg-turbo-devel zlib-devel \
    && yum clean all

# The base image ships pip 24.0, which fails to match many available manylinux
# wheels and falls back to source builds. Upgrading fixes that.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# The base image is Amazon Linux 2 (glibc 2.17, GCC 7.3.1). The newest releases
# of these packages only ship manylinux_2_28 wheels, so pip would source-build
# them (and modern numpy needs GCC >= 9.3, which AL2 lacks). Pin each to a
# version that still publishes a manylinux2014/glibc-2.17 wheel so everything
# installs prebuilt. All are numpy-1.26 compatible; numpy<2 is required by
# paddlepaddle 2.6.2.
RUN pip install --no-cache-dir \
    "pillow==10.4.0" \
    "numpy==1.26.4" \
    "scipy==1.13.1" \
    "scikit-image==0.24.0" \
    "scikit-learn==1.5.2" \
    "matplotlib==3.9.2" \
    "rapidfuzz==3.9.7" \
    paddlepaddle==2.6.2 \
    paddleocr==2.9.1

# Bake the PaddleOCR detection/recognition model weights INTO the image.
# Lambda's runtime filesystem is read-only except /tmp (which starts empty on
# every cold start), so the models must be present at build time. HOME=/opt is
# read-only but readable at runtime, and paddleocr looks in ~/.paddleocr.
ENV HOME=/opt
RUN python -c "from paddleocr import PaddleOCR; PaddleOCR(lang='en', show_log=False)"

# The handler uses relative imports (from .base / .registry), so the package
# must be importable as `ocr.*` — copy the whole package under the task root.
COPY . ${LAMBDA_TASK_ROOT}/ocr

# Default engine for this image. Override per-Lambda if ever needed.
ENV OCR_ENGINE=paddleocr

CMD ["ocr.aws_lambda_handler.lambda_handler"]
