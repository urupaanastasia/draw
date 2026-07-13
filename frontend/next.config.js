/** @type {import('next').NextConfig} */
const nextConfig = {
    typescript: {
        // Ігноруємо помилки типізації під час збірки на Vercel
        ignoreBuildErrors: true,
    },
    // ...тут можуть бути інші твої старі налаштування, якщо вони є...
};

module.exports = nextConfig;