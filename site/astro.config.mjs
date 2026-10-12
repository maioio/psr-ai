import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

export default defineConfig({
  site: 'https://maioio.github.io',
  base: '/psr-ai',
  trailingSlash: 'always',
  integrations: [
    starlight({
      title: 'PSR.ai',
      favicon: '/favicon.ico',
      // The logo already spells PSR.ai, so the title text stays for screen readers only and the image needs no alt.
      logo: { light: './src/assets/logo-horizontal.svg', dark: './src/assets/logo-horizontal-dark.svg', replacesTitle: true },
      head: [
        { tag: 'link', attrs: { rel: 'icon', type: 'image/svg+xml', href: '/psr-ai/favicon.svg' } },
        { tag: 'link', attrs: { rel: 'icon', type: 'image/png', sizes: '32x32', href: '/psr-ai/favicon-32.png' } },
        { tag: 'link', attrs: { rel: 'apple-touch-icon', href: '/psr-ai/apple-touch-icon.png' } },
        { tag: 'meta', attrs: { property: 'og:image:width', content: '1200' } },
        { tag: 'meta', attrs: { property: 'og:image:height', content: '630' } },
        { tag: 'meta', attrs: { name: 'twitter:image', content: 'https://maioio.github.io/psr-ai/social-card.png' } },
        { tag: 'meta', attrs: { property: 'og:image', content: 'https://maioio.github.io/psr-ai/social-card.png' } },
        { tag: 'meta', attrs: { name: 'twitter:card', content: 'summary_large_image' } },
        // Scrollable code blocks and wide tables (scrollable on narrow screens) must be keyboard reachable (axe: scrollable-region-focusable).
        { tag: 'script', content: "document.addEventListener('DOMContentLoaded',()=>document.querySelectorAll('.expressive-code pre, .sl-markdown-content table').forEach(p=>{if(p.scrollWidth>p.clientWidth)p.setAttribute('tabindex','0')}))" },
      ],
      description: 'Record any workflow once. Let AI agents understand it.',
      social: [{ icon: 'github', label: 'GitHub', href: 'https://github.com/maioio/psr-ai' }],
      editLink: { baseUrl: 'https://github.com/maioio/psr-ai/edit/master/site/' },
      customCss: ['./src/styles/theme.css'],
      sidebar: [
        { label: 'Start', items: ['getting-started', 'recording', 'output-format'] },
        { label: 'Use with agents', items: ['mcp', 'hidden-context-layer', 'ai-analysis', 'replay-and-tests'] },
        { label: 'Reference', items: ['commands', 'browser-toolkit', 'platforms', 'privacy-and-security', 'troubleshooting'] },
        { label: 'Project', items: ['architecture', 'limitations', 'contributing'] },
      ],
    }),
  ],
});
