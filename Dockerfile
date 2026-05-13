FROM python:3.9

# نصب پیش‌نیازهای سیستم با نام‌های اصلاح شده
RUN apt-get update && apt-get install -y \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /code

# کپی فایل‌ها و نصب کتابخانه‌های پایتون
COPY ./requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

# نصب هسته مرورگر و پیش‌نیازهای اختصاصی پلی‌رایت
RUN playwright install chromium
RUN playwright install-deps chromium

COPY . .

# اجرای برنامه روی پورت مورد نیاز هاگینگ فیس
CMD ["python", "main.py"]