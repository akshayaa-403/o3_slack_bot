/* ==========================================================================
   The knowledge sources.

   Three tiles, each one sign-in that unlocks a set of products:

     Microsoft 365   -> Workvivo, SharePoint      (one Microsoft sign-in)
     Atlassian       -> Jira, Confluence          (one Atlassian sign-in)
     G Suite         -> Sites, Docs, Sheets        (one Google sign-in)

   A fourth tile, MODEL_SOURCE, is not a content source at all — it lets the
   customer choose which LLM answers (Gemini or Claude). It has no sign-in,
   no ingest, no feeds; each product is a plain on/off toggle gated by an API
   key entered once. It is kept out of SOURCES so nothing that assumes "every
   source is crawled content" (ingest, disconnect-drops-knowledge, the
   summary card's totals) accidentally applies to it.

   Each product/source carries a `line` colour, used as a rule down the edge
   of its tile — and as the colour of the marching-ants status line once
   connected/enabled — plus a hand-drawn `logo` (inline SVG markup) built
   from each brand's published mark, so nothing here depends on loading an
   external image.

   `authorizeUrl` is the provider's real OAuth authorization endpoint. A
   `client_id` still has to be registered with each provider before this can
   complete a live handshake — that is a backend/console step, not a frontend
   one — so the placeholder is left visible in the query string rather than
   hidden behind a fake success.

   `feeds` is what actually gets read into the knowledge base, written in the
   customer's language.
   ========================================================================== */

export const REDIRECT_URI = `${location.origin}${location.pathname.replace(/index\.html$/, '')}popup.html`;

const oauthUrl = (base, params) =>
  `${base}?${new URLSearchParams({ redirect_uri: REDIRECT_URI, ...params }).toString()}`;

/* --- logos ---------------------------------------------------------------
   Real brand marks, redrawn/trimmed to inline SVG so nothing here loads an
   external image. */

const LOGOS = {
  microsoft365: `<svg viewBox="0 0 48 48" fill="none">
    <rect x="2" y="2" width="20" height="20" fill="#F25022"/>
    <rect x="26" y="2" width="20" height="20" fill="#7FBA00"/>
    <rect x="2" y="26" width="20" height="20" fill="#00A4EF"/>
    <rect x="26" y="26" width="20" height="20" fill="#FFB900"/>
  </svg>`,

  workvivo: `<svg viewBox="0 0 320 320" fill="none">
    <rect width="320" height="320" rx="48" fill="#00031F"/>
    <path d="M273.41,152.28c-7.61,3.37-16.51-.09-19.85-7.79,0,0,0-.02-.02-.03l-30.79-71.13c-3.32-7.69.14-16.64,7.76-20.01,7.61-3.36,16.49.14,19.83,7.82l30.85,71.13c3.34,7.69-.14,16.65-7.77,20.01h.02Zm-51.82,56.68c-7.61,3.34-16.48-.17-19.8-7.87l-55.12-127.8c-3.31-7.69.17-16.62,7.8-19.98,7.61-3.36,16.46.16,19.79,7.82v.03l55.08,127.8c3.29,7.68-.17,16.62-7.8,19.98h.03l.02.02Zm-68-8.39c-1.46,3.77-4.32,6.76-7.99,8.37-7.61,3.34-16.48-.16-19.8-7.84v-.02l-31.06-72.27c-3.32-7.69.17-16.64,7.79-19.99,7.61-3.34,16.49.17,19.82,7.87l31.06,72.27c1.61,3.69,1.66,7.88.19,11.62h.02l-.02-.02Zm-82.39-126.87c0-8.91,7.15-16.15,16-16.15s16,7.22,16,16.15-7.17,16.15-16,16.15-16-7.23-16-16.15Z" fill="#0B5CFF"/>
  </svg>`,

  sharepoint: `<svg viewBox="0 0 32 32" fill="none">
    <circle cx="15.5" cy="11.5" r="9.5" fill="url(#sp-g1)"/>
    <circle cx="24" cy="17" r="8" fill="url(#sp-g2)"/>
    <mask id="sp-mask" style="mask-type:alpha" maskUnits="userSpaceOnUse" x="10" y="6" width="13" height="24">
      <path d="M23 23.5C23 27.0899 20.0899 30 16.5 30C12.9101 30 10 27.0899 10 23.5C10 19.9102 10 6 10 6H23C23 6 23 21.1988 23 23.5Z" fill="#C4C4C4"/>
    </mask>
    <g mask="url(#sp-mask)">
      <circle cx="16.5" cy="23.5" r="6.5" fill="url(#sp-g3)"/>
      <path d="M7 12C7 10.3431 8.34315 9 10 9H17C18.6569 9 20 10.3431 20 12V24C20 25.6569 18.6569 27 17 27H7V12Z" fill="#000000" fill-opacity="0.3"/>
    </g>
    <rect y="7" width="18" height="18" rx="2" fill="url(#sp-g4)"/>
    <path d="M13 18.1229C13 16.5726 11.9602 15.8883 9.79665 15.0922C8.10273 14.4637 7.70021 14.2821 7.70021 13.6816C7.70021 13.1648 8.20335 12.8156 9.0587 12.8156C9.93082 12.8156 10.7526 13.1089 11.6751 13.6117L12.6143 11.9497C11.6247 11.3352 10.4507 11 9.02516 11C6.84486 11 5.28512 12.1173 5.28512 13.8212C5.28512 15.567 6.52621 16.1257 8.60587 16.8659C10.2662 17.4525 10.5849 17.7458 10.5849 18.2626C10.5849 18.8771 9.9979 19.1844 9.07547 19.1844C7.98532 19.1844 7.02935 18.8073 6.07338 18.1927L5 19.7849C6.174 20.595 7.63312 21 9.12579 21C11.3732 21 13 19.9385 13 18.1229Z" fill="white"/>
    <defs>
      <linearGradient id="sp-g1" x1="6" y1="11.5" x2="26.5833" y2="11.5" gradientUnits="userSpaceOnUse"><stop stop-color="#103A3B"/><stop offset="1" stop-color="#116B6E"/></linearGradient>
      <linearGradient id="sp-g2" x1="18" y1="13" x2="32" y2="21" gradientUnits="userSpaceOnUse"><stop stop-color="#1D9097"/><stop offset="1" stop-color="#29BBC2"/></linearGradient>
      <linearGradient id="sp-g3" x1="12" y1="21.5" x2="23" y2="26.5" gradientUnits="userSpaceOnUse"><stop stop-color="#28A6B5"/><stop offset="1" stop-color="#31D6EC"/></linearGradient>
      <linearGradient id="sp-g4" x1="0" y1="16" x2="19.5" y2="16" gradientUnits="userSpaceOnUse"><stop stop-color="#105557"/><stop offset="1" stop-color="#116B6E"/></linearGradient>
    </defs>
  </svg>`,

  atlassian: `<svg viewBox="0 0 256 256" fill="none">
    <path d="M75.7929022,117.949352 C71.973435,113.86918 66.0220743,114.100451 63.4262382,119.292123 L0.791180865,244.565041 C-0.370000214,246.886207 -0.24632242,249.643151 1.11803323,251.85102 C2.48238888,254.058889 4.89280393,255.402741 7.48821365,255.402516 L94.716435,255.402516 C97.5716401,255.468706 100.19751,253.845601 101.414869,251.262074 C120.223468,212.37359 108.82814,153.245434 75.7929022,117.949352 Z" fill="url(#atl-g)"/>
    <path d="M121.756071,4.0114918 C86.7234975,59.5164098 89.0348008,120.989508 112.109989,167.141287 L154.170383,251.262074 C155.438703,253.798733 158.031349,255.401095 160.867416,255.401115 L248.094235,255.401115 C250.689645,255.401339 253.10006,254.057487 254.464416,251.849618 C255.828771,249.64175 255.952449,246.884805 254.791268,244.563639 C254.791268,244.563639 137.44462,9.83670492 134.492768,3.96383607 C131.853481,-1.29371311 125.14944,-1.36519672 121.756071,4.0114918 Z" fill="#2681FF"/>
    <defs><linearGradient id="atl-g" x1="99.6865531%" y1="15.8007988%" x2="39.8359011%" y2="97.4378355%" gradientUnits="objectBoundingBox">
      <stop stop-color="#0052CC" offset="0%"/><stop stop-color="#2684FF" offset="92.3%"/>
    </linearGradient></defs>
  </svg>`,

  jira: `<svg viewBox="0 0 256 256" fill="none">
    <path d="M244.657778,0 L121.706667,0 C121.706667,14.7201046 127.554205,28.837312 137.962891,39.2459977 C148.371577,49.6546835 162.488784,55.5022222 177.208889,55.5022222 L199.857778,55.5022222 L199.857778,77.3688889 C199.877391,107.994155 224.699178,132.815943 255.324444,132.835556 L255.324444,10.6666667 C255.324444,4.77562934 250.548815,3.60722001e-16 244.657778,0 Z" fill="#2684FF"/>
    <path d="M183.822222,61.2622222 L60.8711111,61.2622222 C60.8907238,91.8874888 85.7125112,116.709276 116.337778,116.728889 L138.986667,116.728889 L138.986667,138.666667 C139.025905,169.291923 163.863607,194.097803 194.488889,194.097778 L194.488889,71.9288889 C194.488889,66.0378516 189.71326,61.2622222 183.822222,61.2622222 Z" fill="url(#jira-g1)"/>
    <path d="M122.951111,122.488889 L0,122.488889 C3.75391362e-15,153.14192 24.8491913,177.991111 55.5022222,177.991111 L78.2222222,177.991111 L78.2222222,199.857778 C78.241767,230.45532 103.020285,255.265647 133.617778,255.324444 L133.617778,133.155556 C133.617778,127.264518 128.842148,122.488889 122.951111,122.488889 Z" fill="url(#jira-g2)"/>
    <defs>
      <linearGradient id="jira-g1" x1="98.0308675%" y1="0.160599572%" x2="58.8877062%" y2="40.7655246%" gradientUnits="objectBoundingBox">
        <stop stop-color="#0052CC" offset="18%"/><stop stop-color="#2684FF" offset="100%"/>
      </linearGradient>
      <linearGradient id="jira-g2" x1="100.665247%" y1="0.45503212%" x2="55.4018095%" y2="44.7269807%" gradientUnits="objectBoundingBox">
        <stop stop-color="#0052CC" offset="18%"/><stop stop-color="#2684FF" offset="100%"/>
      </linearGradient>
    </defs>
  </svg>`,

  confluence: `<svg viewBox="0 0 32 32" fill="none">
    <path d="M3.015,23.087c-.289.472-.614,1.02-.891,1.456a.892.892,0,0,0,.3,1.212l5.792,3.564a.89.89,0,0,0,1.226-.29l.008-.013c.231-.387.53-.891.855-1.43,2.294-3.787,4.6-3.323,8.763-1.336l5.743,2.731A.892.892,0,0,0,26,28.559l.011-.024L28.766,22.3a.891.891,0,0,0-.445-1.167c-1.212-.57-3.622-1.707-5.792-2.754C14.724,14.586,8.09,14.831,3.015,23.087Z" fill="url(#conf-g1)"/>
    <path d="M28.985,8.932c.289-.472.614-1.02.891-1.456a.892.892,0,0,0-.3-1.212L23.785,2.7a.89.89,0,0,0-1.236.241.584.584,0,0,0-.033.053c-.232.387-.53.891-.856,1.43-2.294,3.787-4.6,3.323-8.763,1.336L7.172,3.043a.89.89,0,0,0-1.187.421l-.011.024L3.216,9.726a.891.891,0,0,0,.445,1.167c1.212.57,3.622,1.706,5.792,2.753C17.276,17.433,23.91,17.179,28.985,8.932Z" fill="url(#conf-g2)"/>
    <defs>
      <linearGradient id="conf-g1" x1="28.607" y1="-30.825" x2="11.085" y2="-20.756" gradientUnits="userSpaceOnUse">
        <stop offset="0.18" stop-color="#0052cc"/><stop offset="1" stop-color="#2684ff"/>
      </linearGradient>
      <linearGradient id="conf-g2" x1="3.388" y1="0.857" x2="20.915" y2="10.93" gradientUnits="userSpaceOnUse">
        <stop offset="0.18" stop-color="#0052cc"/><stop offset="1" stop-color="#2684ff"/>
      </linearGradient>
    </defs>
  </svg>`,

  gsuite: `<svg viewBox="-0.5 0 48 48" fill="none">
    <path d="M9.82727273,24 C9.82727273,22.4757333 10.0804318,21.0144 10.5322727,19.6437333 L2.62345455,13.6042667 C1.08206818,16.7338667 0.213636364,20.2602667 0.213636364,24 C0.213636364,27.7365333 1.081,31.2608 2.62025,34.3882667 L10.5247955,28.3370667 C10.0772273,26.9728 9.82727273,25.5168 9.82727273,24" fill="#FBBC05"/>
    <path d="M23.7136364,10.1333333 C27.025,10.1333333 30.0159091,11.3066667 32.3659091,13.2266667 L39.2022727,6.4 C35.0363636,2.77333333 29.6954545,0.533333333 23.7136364,0.533333333 C14.4268636,0.533333333 6.44540909,5.84426667 2.62345455,13.6042667 L10.5322727,19.6437333 C12.3545909,14.112 17.5491591,10.1333333 23.7136364,10.1333333" fill="#EB4335"/>
    <path d="M23.7136364,37.8666667 C17.5491591,37.8666667 12.3545909,33.888 10.5322727,28.3562667 L2.62345455,34.3946667 C6.44540909,42.1557333 14.4268636,47.4666667 23.7136364,47.4666667 C29.4455,47.4666667 34.9177955,45.4314667 39.0249545,41.6181333 L31.5177727,35.8144 C29.3995682,37.1488 26.7323182,37.8666667 23.7136364,37.8666667" fill="#34A853"/>
    <path d="M46.1454545,24 C46.1454545,22.6133333 45.9318182,21.12 45.6113636,19.7333333 L23.7136364,19.7333333 L23.7136364,28.8 L36.3181818,28.8 C35.6879545,31.8912 33.9724545,34.2677333 31.5177727,35.8144 L39.0249545,41.6181333 C43.3393409,37.6138667 46.1454545,31.6490667 46.1454545,24" fill="#4285F4"/>
  </svg>`,

  googleSites: `<svg viewBox="0 0 192 192" fill="none">
    <mask id="gs-mask" width="168" height="140" x="12" y="26" maskUnits="userSpaceOnUse" style="mask-type:alpha">
      <path fill="#579fff" d="M12 64.48c0-11.627 0-17.44 1.826-22.051a26 26 0 0 1 14.603-14.603C33.04 26 38.853 26 50.48 26h91.04c11.627 0 17.44 0 22.051 1.826a26 26 0 0 1 14.603 14.603C180 47.04 180 52.853 180 64.48v63.04c0 11.627 0 17.44-1.826 22.051a26 26 0 0 1-14.603 14.603C158.96 166 153.147 166 141.52 166H50.48c-11.626 0-17.44 0-22.051-1.826a26 26 0 0 1-14.603-14.603C12 144.96 12 139.147 12 127.52z"/>
    </mask>
    <g mask="url(#gs-mask)">
      <path fill="#4fa0ff" d="M12 26h168v140H12z"/>
      <rect width="30" height="26" x="132" y="122" fill="#fff" rx="13"/>
      <path fill="#9dd2ff" d="M62 70H12v71c0 13.807 11.193 25 25 25s25-11.193 25-25z"/>
      <path fill="#3186ff" d="M12 26h168v44H12z"/>
    </g>
    <mask id="gs-mask2" width="166" height="44" x="12" y="26" maskUnits="userSpaceOnUse" style="mask-type:alpha">
      <path fill="#3983ff" d="M12 64.48c0-11.627 0-17.44 1.826-22.051a26 26 0 0 1 14.603-14.603C33.04 26 38.853 26 50.48 26h89.04c11.627 0 17.44 0 22.051 1.826a26 26 0 0 1 14.603 14.603C178 47.04 178 52.853 178 64.48V70H12z"/>
    </mask>
    <g filter="url(#gs-blur)" mask="url(#gs-mask2)">
      <path fill="url(#gs-grad)" d="M4 26h58v140H4z"/>
    </g>
    <defs>
      <linearGradient id="gs-grad" x1="40.03" x2="67.89" y1="70.55" y2="30.56" gradientUnits="userSpaceOnUse">
        <stop stop-color="#a9a8ff"/><stop offset="1" stop-color="#336ef3"/>
      </linearGradient>
      <filter id="gs-blur" x="-8.8" y="13.2" width="83.6" height="165.6" color-interpolation-filters="sRGB" filterUnits="userSpaceOnUse">
        <feFlood flood-opacity="0" result="bg"/><feBlend in="SourceGraphic" in2="bg" result="shape"/>
        <feGaussianBlur stdDeviation="6.4"/>
      </filter>
    </defs>
  </svg>`,

  googleDocs: `<svg viewBox="0 0 192 192" fill="none">
    <mask id="gd-mask" style="mask-type:alpha" maskUnits="userSpaceOnUse" x="32" y="8" width="128" height="176">
      <path d="M130.334 184L61.6 184C52.6565 184 48.1848 184 44.6375 182.596C39.5029 180.563 35.4374 176.497 33.4045 171.362C32 167.815 32 163.343 32 154.4L32 37.6C32 28.6565 32 24.1848 33.4045 20.6375C35.4374 15.5029 39.5029 11.4374 44.6375 9.40447C48.1848 8 52.6565 8 61.6 8L100 8L154.793 62.7933L154.793 62.7934C156.454 64.4543 157.285 65.2848 157.923 66.2239C158.845 67.5811 159.479 69.1131 159.785 70.725C159.997 71.8404 159.997 73.0264 159.995 75.3985C159.96 124.317 159.938 124.799 159.937 154.366C159.937 163.332 159.937 167.816 158.532 171.363C156.499 176.498 152.434 180.562 147.299 182.596C143.752 184 139.279 184 130.334 184Z" fill="#3186FF"/>
    </mask>
    <g mask="url(#gd-mask)">
      <path d="M159.94 184L31.9999 184L31.9999 8.00001L99.9999 8L159.999 68L159.94 184Z" fill="#3186FF"/>
      <g filter="url(#gd-blur)">
        <path d="M43 192H149V70.2271V20H43V192Z" fill="url(#gd-grad)"/>
      </g>
    </g>
    <path d="M154.995 62.9951C152.489 61.1143 149.375 60 146 60H112.8C105.731 60 100 54.2692 100 47.2002V8L154.995 62.9951Z" fill="#76BBFF"/>
    <rect x="64.001" y="114" width="64" height="12" rx="6" fill="white"/>
    <rect x="64.001" y="143" width="48" height="12" rx="6" fill="white"/>
    <defs>
      <filter id="gd-blur" x="31" y="8" width="130" height="196" filterUnits="userSpaceOnUse" color-interpolation-filters="sRGB">
        <feFlood flood-opacity="0" result="bg"/><feBlend in="SourceGraphic" in2="bg" result="shape"/>
        <feGaussianBlur stdDeviation="6"/>
      </filter>
      <linearGradient id="gd-grad" x1="96" y1="59.2839" x2="54.6124" y2="171.338" gradientUnits="userSpaceOnUse">
        <stop offset="0.33" stop-color="#3186FF"/><stop offset="1" stop-color="#A9A8FF"/>
      </linearGradient>
    </defs>
  </svg>`,

  googleSheets: `<svg viewBox="0 0 192 192" fill="none">
    <path fill="#009954" d="M8 74.6c0-8.943 0-13.415 1.404-16.962a20 20 0 0 1 11.234-11.233C24.185 45 28.656 45 37.6 45h60.8c8.943 0 13.415 0 16.962 1.404a20 20 0 0 1 11.234 11.234C128 61.185 128 65.656 128 74.6v42.8c0 8.943 0 13.415-1.404 16.962a20 20 0 0 1-11.234 11.234C111.815 147 107.343 147 98.4 147H37.6c-8.943 0-13.415 0-16.963-1.404a20 20 0 0 1-11.233-11.234C8 130.815 8 126.343 8 117.4z"/>
    <mask id="gsh-mask" width="160" height="128" x="24" y="32" maskUnits="userSpaceOnUse" style="mask-type:alpha"><rect width="160" height="128" x="24" y="32" fill="#0ebc5f" rx="20"/></mask>
    <g mask="url(#gsh-mask)">
      <path fill="#0ebc5f" d="M24 32h160v128H24z"/>
      <g filter="url(#gsh-blur)">
        <rect width="144" height="102" fill="url(#gsh-grad)" rx="25.6" transform="matrix(1 0 0 -1 8 147)"/>
      </g>
    </g>
    <path stroke="#fff" stroke-linecap="round" stroke-width="12" d="M80 121h84m-20 19V76"/>
    <defs>
      <linearGradient id="gsh-grad" x1="122.24" x2="20.76" y1="43.31" y2="43.31" gradientUnits="userSpaceOnUse"><stop stop-color="#0ebc5f"/><stop offset=".95" stop-color="#78c9ff"/></linearGradient>
      <filter id="gsh-blur" x="-4" y="33" width="168" height="126" color-interpolation-filters="sRGB" filterUnits="userSpaceOnUse">
        <feFlood flood-opacity="0" result="bg"/><feBlend in="SourceGraphic" in2="bg" result="shape"/><feGaussianBlur stdDeviation="6"/>
      </filter>
    </defs>
  </svg>`,

  gemini: `<svg viewBox="0 0 65 65" fill="none">
    <path d="M32.447 0c.68 0 1.273.465 1.439 1.125a38.904 38.904 0 001.999 5.905c2.152 5 5.105 9.376 8.854 13.125 3.751 3.75 8.126 6.703 13.125 8.855a38.98 38.98 0 005.906 1.999c.66.166 1.124.758 1.124 1.438 0 .68-.464 1.273-1.125 1.439a38.902 38.902 0 00-5.905 1.999c-5 2.152-9.375 5.105-13.125 8.854-3.749 3.751-6.702 8.126-8.854 13.125a38.973 38.973 0 00-2 5.906 1.485 1.485 0 01-1.438 1.124c-.68 0-1.272-.464-1.438-1.125a38.913 38.913 0 00-2-5.905c-2.151-5-5.103-9.375-8.854-13.125-3.75-3.749-8.125-6.702-13.125-8.854a38.973 38.973 0 00-5.905-2A1.485 1.485 0 010 32.448c0-.68.465-1.272 1.125-1.438a38.903 38.903 0 005.905-2c5-2.151 9.376-5.104 13.125-8.854 3.75-3.749 6.703-8.125 8.855-13.125a38.972 38.972 0 001.999-5.905A1.485 1.485 0 0132.447 0z" fill="url(#gem-grad)"/>
    <defs>
      <linearGradient id="gem-grad" x1="18.447" y1="43.42" x2="52.153" y2="15.004" gradientUnits="userSpaceOnUse">
        <stop stop-color="#4893FC"/><stop offset=".27" stop-color="#4893FC"/><stop offset=".777" stop-color="#969DFF"/><stop offset="1" stop-color="#BD99FE"/>
      </linearGradient>
    </defs>
  </svg>`,

  claude: `<svg viewBox="0 0 100 100" fill="#D97757">
    <path d="m19.6 66.5 19.7-11 .3-1-.3-.5h-1l-3.3-.2-11.2-.3L14 53l-9.5-.5-2.4-.5L0 49l.2-1.5 2-1.3 2.9.2 6.3.5 9.5.6 6.9.4L38 49.1h1.6l.2-.7-.5-.4-.4-.4L29 41l-10.6-7-5.6-4.1-3-2-1.5-2-.6-4.2 2.7-3 3.7.3.9.2 3.7 2.9 8 6.1L37 36l1.5 1.2.6-.4.1-.3-.7-1.1L33 25l-6-10.4-2.7-4.3-.7-2.6c-.3-1-.4-2-.4-3l3-4.2L28 0l4.2.6L33.8 2l2.6 6 4.1 9.3L47 29.9l2 3.8 1 3.4.3 1h.7v-.5l.5-7.2 1-8.7 1-11.2.3-3.2 1.6-3.8 3-2L61 2.6l2 2.9-.3 1.8-1.1 7.7L59 27.1l-1.5 8.2h.9l1-1.1 4.1-5.4 6.9-8.6 3-3.5L77 13l2.3-1.8h4.3l3.1 4.7-1.4 4.9-4.4 5.6-3.7 4.7-5.3 7.1-3.2 5.7.3.4h.7l12-2.6 6.4-1.1 7.6-1.3 3.5 1.6.4 1.6-1.4 3.4-8.2 2-9.6 2-14.3 3.3-.2.1.2.3 6.4.6 2.8.2h6.8l12.6 1 3.3 2 1.9 2.7-.3 2-5.1 2.6-6.8-1.6-16-3.8-5.4-1.3h-.8v.4l4.6 4.5 8.3 7.5L89 80.1l.5 2.4-1.3 2-1.4-.2-9.2-7-3.6-3-8-6.8h-.5v.7l1.8 2.7 9.8 14.7.5 4.5-.7 1.4-2.6 1-2.7-.6-5.8-8-6-9-4.7-8.2-.5.4-2.9 30.2-1.3 1.5-3 1.2-2.5-2-1.4-3 1.4-6.2 1.6-8 1.3-6.4 1.2-7.9.7-2.6v-.2H49L43 72l-9 12.3-7.2 7.6-1.7.7-3-1.5.3-2.8L24 86l10-12.8 6-7.9 4-4.6-.1-.5h-.3L17.2 77.4l-4.7.6-2-2 .2-3 1-1 8-5.5Z"/>
  </svg>`,

  servicenow: `<svg viewBox="0 0 685.1 100" fill="none">
    <path d="M164.9,30.8c-6.9,0-13.6,2.4-18.9,6.8v-6.1h-17.2v67.1h17.9V55.7c3.9-5.1,9.9-8.1,16.3-8.3c2.4-0.1,4.9,0.2,7.2,1.1V31.3C168.4,31,166.6,30.8,164.9,30.8" fill="#293e40"/>
    <path d="M8.7,78.1c5.1,4.3,11.6,6.7,18.3,6.6c4.8,0,8.5-2.4,8.5-5.7C35.5,69,3.1,72.6,3.1,51c0-12.9,12.4-20.9,25.6-20.9c8,0,15.9,2.4,22.6,6.8l-8.4,13c-3.7-2.8-8.2-4.4-12.8-4.6c-5,0-9.1,1.9-9.1,5.4c0,8.7,32.4,5.3,32.4,28.5c0,12.9-12.6,20.7-26.6,20.7C17.2,99.9,7.8,96.7,0,91L8.7,78.1z" fill="#293e40"/>
    <path d="M121.1,64.4c0-18.7-13.1-34.3-31.6-34.3c-19.8,0-32.5,16.3-32.5,35c-0.8,18.5,13.6,34.1,32,34.9c1,0,2,0,3,0c10.4,0.1,20.4-4.2,27.4-12l-10.2-10.2c-4.3,4.8-10.4,7.6-16.8,7.7c-9.3,0.3-17.2-6.7-18.1-15.9h46.3C121,67.9,121.1,66.1,121.1,64.4z M75.2,56.4c1.3-6.9,7.3-11.8,14.3-11.8c6.7,0,12.4,5.1,13.2,11.8H75.2z" fill="#293e40"/>
    <path d="M212.8,72.4 231.1,31.5 249.7,31.5 219,98.6 206.6,98.6 175.9,31.5 194.5,31.5Z" fill="#293e40"/>
    <path d="M264.8,0c6.4,0.1,11.4,5.4,11.3,11.7c-0.1,6.4-5.4,11.4-11.7,11.3c-6.3-0.1-11.3-5.2-11.3-11.5c0-6.4,5.1-11.5,11.5-11.5C264.6,0,264.7,0,264.8,0" fill="#293e40"/>
    <rect x="255.8" y="31.5" width="17.9" height="67.1" fill="#293e40"/>
    <path d="M347.8,84.9c-6.9,9.9-18.3,15.5-30.3,15.1c-19.3,0.5-35.3-14.8-35.8-34s14.8-35.3,34-35.8c0.6,0,1.3,0,1.9,0c10.9-0.1,21.3,4.9,28.1,13.4L333,54.7c-3.6-5-9.3-7.9-15.4-8c-9.9,0-18,8-18,17.9c0,0.2,0,0.3,0,0.5c-0.3,9.8,7.3,17.9,17.1,18.3c0.5,0,0.9,0,1.4,0c6.5-0.1,12.4-3.4,16-8.8L347.8,84.9z" fill="#293e40"/>
    <path d="M412.8,87.9c-7,7.8-17,12.2-27.4,12c-18.5,0.9-34.1-13.4-35-31.9c0-1,0-2,0-3c0-18.7,12.7-35,32.5-35c18.5,0,31.6,15.6,31.6,34.3c0,1.7-0.1,3.4-0.4,5.1h-46.3c0.9,9.2,8.8,16.2,18.1,15.9c6.4-0.2,12.5-3,16.8-7.7L412.8,87.9z M396.2,56.4c-0.8-6.7-6.5-11.7-13.2-11.8c-7,0-13,4.9-14.3,11.8H396.2z" fill="#293e40"/>
    <path d="M421.6,98.6V31.5h17.2v5.4c5.3-4.4,12-6.8,18.9-6.8c8.9,0,17.4,3.9,23.2,10.8c5.2,6.7,7.6,15.1,6.9,23.5v34.1h-17.9V63c0.5-4.6-0.9-9.1-4-12.6c-2.7-2.5-6.3-3.9-10.1-3.7c-6.4,0.2-12.4,3.2-16.3,8.3v43.6H421.6z" fill="#293e40"/>
    <path d="M533.9,30.1c-21.7,0-39.3,17.5-39.3,39.1c0,10.8,4.4,21.2,12.3,28.6c2.8,2.6,7,2.9,10.1,0.5c9.9-7.4,23.5-7.4,33.4,0c3.1,2.3,7.4,2.1,10.1-0.6c15.7-14.9,16.4-39.7,1.5-55.5C554.6,34.6,544.5,30.2,533.9,30.1 M533.7,88.9c-10.5,0.3-19.2-8-19.5-18.5c0-0.3,0-0.7,0-1c0-10.8,8.7-19.5,19.5-19.5s19.5,8.7,19.5,19.5c0.3,10.5-8,19.2-18.5,19.5C534.3,88.9,534,88.9,533.7,88.9" fill="#62D84E"/>
    <path d="M608.2,98.6 594.8,98.6 568.2,31.5 586.1,31.5 600.7,69.8 615,31.5 629.9,31.5 644.1,69.8 658.8,31.5 676.7,31.5 650.1,98.6 636.8,98.6 622.5,60.4Z" fill="#293e40"/>
  </svg>`,

  slack: `<svg viewBox="0 0 32 32" fill="none">
    <path d="M26.5002 14.9996C27.8808 14.9996 29 13.8804 29 12.4998C29 11.1192 27.8807 10 26.5001 10C25.1194 10 24 11.1193 24 12.5V14.9996H26.5002ZM19.5 14.9996C20.8807 14.9996 22 13.8803 22 12.4996V5.5C22 4.11929 20.8807 3 19.5 3C18.1193 3 17 4.11929 17 5.5V12.4996C17 13.8803 18.1193 14.9996 19.5 14.9996Z" fill="#2EB67D"/>
    <path d="M5.49979 17.0004C4.11919 17.0004 3 18.1196 3 19.5002C3 20.8808 4.1193 22 5.49989 22C6.8806 22 8 20.8807 8 19.5V17.0004H5.49979ZM12.5 17.0004C11.1193 17.0004 10 18.1197 10 19.5004V26.5C10 27.8807 11.1193 29 12.5 29C13.8807 29 15 27.8807 15 26.5V19.5004C15 18.1197 13.8807 17.0004 12.5 17.0004Z" fill="#E01E5A"/>
    <path d="M17.0004 26.5002C17.0004 27.8808 18.1196 29 19.5002 29C20.8808 29 22 27.8807 22 26.5001C22 25.1194 20.8807 24 19.5 24L17.0004 24L17.0004 26.5002ZM17.0004 19.5C17.0004 20.8807 18.1197 22 19.5004 22L26.5 22C27.8807 22 29 20.8807 29 19.5C29 18.1193 27.8807 17 26.5 17L19.5004 17C18.1197 17 17.0004 18.1193 17.0004 19.5Z" fill="#ECB22E"/>
    <path d="M14.9996 5.49979C14.9996 4.11919 13.8804 3 12.4998 3C11.1192 3 10 4.1193 10 5.49989C10 6.88061 11.1193 8 12.5 8L14.9996 8L14.9996 5.49979ZM14.9996 12.5C14.9996 11.1193 13.8803 10 12.4996 10L5.5 10C4.11929 10 3 11.1193 3 12.5C3 13.8807 4.11929 15 5.5 15L12.4996 15C13.8803 15 14.9996 13.8807 14.9996 12.5Z" fill="#36C5F0"/>
  </svg>`,

  zendesk: `<svg viewBox="0 0 78.4 56" fill="#03363D">
    <path d="M37.5 8.8v24.4H17.4zm0-8.8c0 5.6-4.5 10.1-10.1 10.1s-10-4.5-10-10.1zm3.3 33.1c0-5.6 4.5-10.1 10.1-10.1S61 27.5 61 33.1zm0-8.7V0H61z"/>
  </svg>`,

  notion: `<svg viewBox="0 0 59.9 62.6" fill="none">
    <path d="M3.8,2.7l34.6-2.6c4.2-0.4,5.3-0.1,8,1.8l11.1,7.8c1.8,1.3,2.4,1.7,2.4,3.2v42.7c0,2.7-1,4.3-4.4,4.5l-40.2,2.4c-2.6,0.1-3.8-0.2-5.1-1.9L2.1,50.1c-1.5-2-2.1-3.4-2.1-5.1V7C0,4.8,1,2.9,3.8,2.7L3.8,2.7z" fill="#fff"/>
    <path d="M38.4,0.1L3.8,2.7C1,2.9,0,4.8,0,7v38c0,1.7,0.6,3.2,2.1,5.1l8.1,10.6c1.3,1.7,2.6,2.1,5.1,1.9l40.2-2.4c3.4-0.2,4.4-1.8,4.4-4.5V12.9c0-1.4-0.5-1.8-2.2-3c-0.1-0.1-0.2-0.1-0.3-0.2L46.4,2C43.8,0,42.7-0.2,38.4,0.1L38.4,0.1z M16.2,12.2c-3.3,0.2-4,0.3-5.9-1.3L5.6,7.2C5.1,6.7,5.3,6.1,6.6,6l33.3-2.4c2.8-0.2,4.2,0.7,5.3,1.6l5.7,4.1c0.3,0.1,0.9,0.8,0.1,0.8l-34.4,2.1L16.2,12.2z M12.4,55.3V19c0-1.6,0.5-2.3,1.9-2.4l39.5-2.3c1.3-0.1,1.9,0.7,1.9,2.3v36c0,1.6-0.3,2.9-2.4,3l-37.8,2.2C13.4,58,12.4,57.2,12.4,55.3L12.4,55.3z M49.7,21c0.2,1.1,0,2.2-1.1,2.3l-1.8,0.4v26.8c-1.6,0.9-3,1.3-4.2,1.3c-1.9,0-2.4-0.6-3.9-2.4L26.7,30.6v18.1l3.8,0.9c0,0,0,2.2-3,2.2l-8.4,0.5c-0.2-0.5,0-1.7,0.8-1.9l2.2-0.6v-24l-3-0.3c-0.2-1.1,0.4-2.7,2.1-2.8l9-0.6l12.4,19V24.3l-3.2-0.4c-0.2-1.3,0.7-2.3,1.9-2.4L49.7,21z" fill="#000"/>
  </svg>`,

  upload: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
    <path d="M12 15V3m0 0-4 4m4-4 4 4" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="M4 15v3a3 3 0 0 0 3 3h10a3 3 0 0 0 3-3v-3" stroke-linecap="round" stroke-linejoin="round"/>
  </svg>`,

  googlechat: `<svg viewBox="0 0 192 192" fill="none">
    <rect width="160" height="96" x="16" y="28" fill="#00AF57" rx="48"/>
    <path fill="#0EBC5F" d="M133 48c28.167 0 51 22.834 51 51 0 28.167-22.833 51-51 51H96.624l-34.857 23.064c-3.86 2.544-5.789 3.816-7.372 3.92a6 6 0 0 1-5.612-3.022C48 172.583 48 170.271 48 165.649V148.81C25.121 143.78 8 123.39 8 99c0-28.166 22.834-51 51-51h74z"/>
    <mask id="gc-mask" width="176" height="129" x="8" y="48" maskUnits="userSpaceOnUse" style="mask-type:alpha">
      <path fill="#0EBC5F" d="M133 48c28.167 0 51 22.834 51 51 0 28.167-22.833 51-51 51H96.722l-39.428 25.896c-3.99 2.62-9.294-.242-9.294-5.015V148.81C25.121 143.78 8 123.39 8 99c0-28.166 22.834-51 51-51h74z"/>
    </mask>
    <g mask="url(#gc-mask)">
      <rect width="160" height="96" x="16" y="28" fill="#0EBC5F" rx="48"/>
      <rect width="160" height="96" x="16" y="28" fill="url(#gc-grad)" rx="48"/>
      <path stroke="#fff" stroke-linecap="round" stroke-width="12" d="M62 94s8.84 18 34 18 34-17.182 34-17.182"/>
    </g>
    <defs>
      <linearGradient id="gc-grad" x1="96" x2="96" y1="28" y2="124" gradientUnits="userSpaceOnUse">
        <stop offset=".09" stop-color="#94D4FF"/><stop offset=".28" stop-color="#78C9FF"/><stop offset=".88" stop-color="#01AE58" stop-opacity="0"/>
      </linearGradient>
    </defs>
  </svg>`,

  crawl: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
    <circle cx="12" cy="12" r="9" stroke-linecap="round"/>
    <path d="M3 12h18M12 3a13.5 13.5 0 0 1 0 18M12 3a13.5 13.5 0 0 0 0 18" stroke-linecap="round"/>
  </svg>`,

};

/* --- Microsoft 365: Workvivo + SharePoint behind one Microsoft sign-in --- */

export const MICROSOFT_PRODUCTS = [
  {
    id: 'workvivo',
    name: 'Workvivo',
    logo: LOGOS.workvivo,
    blurb: 'Posts and announcements published to your intranet feed.',
    feeds: ['Workvivo posts, announcements and comments'],
    units: ['posts'],
    scope: 'EngagementSpace.Read.All',
  },
  {
    id: 'sharepoint',
    name: 'SharePoint',
    logo: LOGOS.sharepoint,
    blurb: 'Documents your team already keeps in SharePoint and OneDrive.',
    feeds: ['SharePoint site documents', 'Text inside screenshots and scans'],
    units: ['documents'],
    scope: 'Sites.Read.All',
  },
];

/* --- Atlassian: Jira + Confluence behind one Atlassian sign-in --- */

export const ATLASSIAN_PRODUCTS = [
  {
    id: 'jira',
    name: 'Jira',
    logo: LOGOS.jira,
    blurb: 'Resolved issues and the comments that closed them.',
    feeds: ['Resolved Jira issues and their resolution comments'],
    units: ['tickets'],
    scope: 'read:jira-work',
  },
  {
    id: 'confluence',
    name: 'Confluence',
    logo: LOGOS.confluence,
    blurb: 'Pages and diagrams your team has already written up.',
    feeds: ['Confluence pages, including diagrams'],
    units: ['pages'],
    scope: 'read:confluence-content.summary',
  },
];

/* --- G Suite: Sites + Docs + Sheets behind one Google sign-in --- */

export const GOOGLE_PRODUCTS = [
  {
    id: 'sites',
    name: 'Google Sites',
    logo: LOGOS.googleSites,
    blurb: 'Pages published to your team’s internal Sites.',
    feeds: ['Google Sites pages'],
    units: ['pages'],
    scope: 'https://www.googleapis.com/auth/drive.readonly',
  },
  {
    id: 'docs',
    name: 'Google Docs',
    logo: LOGOS.googleDocs,
    blurb: 'Documents your team has already written up.',
    feeds: ['Google Docs files'],
    units: ['documents'],
    scope: 'https://www.googleapis.com/auth/documents.readonly',
  },
  {
    id: 'sheets',
    name: 'Google Sheets',
    logo: LOGOS.googleSheets,
    blurb: 'Trackers and reference tables your team maintains.',
    feeds: ['Google Sheets files'],
    units: ['sheets'],
    scope: 'https://www.googleapis.com/auth/spreadsheets.readonly',
  },
];

export const SOURCES = [
  {
    id: 'atlassian',
    kind: 'Ticketing & docs',
    name: 'Atlassian',
    line: '#0052CC',
    logo: LOGOS.atlassian,
    blurb: 'One sign-in covers Jira and Confluence — choose either or both once you’re in.',
    products: ATLASSIAN_PRODUCTS,
    authorizeUrl: (clientId, state) => oauthUrl('https://auth.atlassian.com/authorize', {
      audience: 'api.atlassian.com',
      client_id: clientId,
      scope: ATLASSIAN_PRODUCTS.map((p) => p.scope).join(' '),
      response_type: 'code',
      prompt: 'consent',
      state,
    }),
  },
  {
    id: 'microsoft365',
    kind: 'Intranet & docs',
    name: 'Microsoft 365',
    line: '#5059C9',
    logo: LOGOS.microsoft365,
    blurb: 'One sign-in covers Workvivo and SharePoint — choose either or both once you’re in.',
    products: MICROSOFT_PRODUCTS,
    authorizeUrl: (clientId, state) => oauthUrl(
      'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
      {
        client_id: clientId,
        response_type: 'code',
        scope: `offline_access ${MICROSOFT_PRODUCTS.map((p) => p.scope).join(' ')}`,
        state,
      },
    ),
  },
  {
    id: 'gsuite',
    kind: 'Documents',
    name: 'G Suite',
    line: '#4285F4',
    logo: LOGOS.gsuite,
    blurb: 'One sign-in covers Sites, Docs and Sheets — choose any or all once you’re in.',
    products: GOOGLE_PRODUCTS,
    authorizeUrl: (clientId, state) => oauthUrl('https://accounts.google.com/o/oauth2/v2/auth', {
      client_id: clientId,
      response_type: 'code',
      scope: GOOGLE_PRODUCTS.map((p) => p.scope).join(' '),
      access_type: 'offline',
      prompt: 'consent',
      state,
    }),
  },
];

export const sourceById = (id) =>
  SOURCES.find((s) => s.id === id) || MORE_CONNECTORS.find((s) => s.id === id);

/* --- More connectors: not yet built, shown as placeholders --------------
   Recommended next by a connector survey (see research/), sitting behind a
   "More connectors" toggle so the four working tiles above aren't crowded
   by eight that don't do anything yet. Each still gets a real authorizeUrl
   where the provider's OAuth shape is already known — clicking "Connect"
   opens the same popup flow as a working tile, just with no client_id
   registered, so it's honest about being unfinished rather than inert. */

export const MORE_CONNECTORS = [
  {
    id: 'servicenow',
    kind: 'Ticketing',
    name: 'ServiceNow',
    line: '#81B5A1',
    logo: LOGOS.servicenow,
    blurb: 'Read closed incidents and the resolution notes attached to them.',
    products: [{
      id: 'incidents',
      name: 'Incidents',
      logo: LOGOS.servicenow,
      blurb: 'Closed incidents and their resolution notes.',
      feeds: ['Closed incidents and requests', 'Resolution notes and work-arounds'],
      units: ['incidents'],
    }],
    authorizeUrl: () => 'https://account.servicenow.com/sign-in',
  },
  {
    id: 'slackhistory',
    kind: 'Conversations',
    name: 'Slack history',
    line: '#36C5F0',
    logo: LOGOS.slack,
    blurb: 'The answers people already gave each other, in channels I can see.',
    products: [{
      id: 'threads',
      name: 'Channel history',
      logo: LOGOS.slack,
      blurb: 'Questions and the replies that resolved them.',
      feeds: ['Questions and the replies that resolved them'],
      units: ['threads'],
    }],
    authorizeUrl: (clientId, state) => oauthUrl('https://slack.com/oauth/v2/authorize', {
      client_id: clientId,
      scope: 'channels:history,channels:read,groups:history',
      response_type: 'code',
      state,
    }),
  },
  {
    id: 'zendesk',
    kind: 'Ticketing',
    name: 'Zendesk',
    line: '#03363D',
    logo: LOGOS.zendesk,
    blurb: 'Resolved tickets and the replies that closed them.',
    products: [{
      id: 'tickets',
      name: 'Tickets',
      logo: LOGOS.zendesk,
      blurb: 'Solved tickets and their resolving replies.',
      feeds: ['Solved tickets and the replies that closed them'],
      units: ['tickets'],
    }],
    authorizeUrl: (clientId, state) => oauthUrl('https://{your-subdomain}.zendesk.com/oauth/authorizations/new', {
      client_id: clientId,
      response_type: 'code',
      scope: 'tickets:read',
      state,
    }),
  },
  {
    id: 'notion',
    kind: 'Documents',
    name: 'Notion',
    line: '#000000',
    logo: LOGOS.notion,
    blurb: 'Pages and databases your team already keeps in Notion.',
    products: [{
      id: 'pages',
      name: 'Pages',
      logo: LOGOS.notion,
      blurb: 'Pages and databases shared with this integration.',
      feeds: ['Notion pages and databases'],
      units: ['pages'],
    }],
    authorizeUrl: (clientId, state) => oauthUrl('https://api.notion.com/v1/oauth/authorize', {
      client_id: clientId,
      response_type: 'code',
      owner: 'user',
      state,
    }),
  },
  {
    id: 'upload',
    kind: 'Files',
    name: 'Upload files',
    line: '#8B8B94',
    logo: LOGOS.upload,
    blurb: 'Drop in the documents that do not live in any connected system.',
    products: [{
      id: 'files',
      name: 'Uploaded files',
      logo: LOGOS.upload,
      blurb: 'PDFs, docs, and one-off files you upload directly.',
      feeds: ['Files you upload'],
      units: ['files'],
    }],
    authorizeUrl: () => null,
  },
  {
    id: 'googlechat',
    kind: 'Conversations',
    name: 'Google Chat history',
    line: '#00AC47',
    logo: LOGOS.googlechat,
    blurb: 'The answers people already gave each other, in spaces I can see.',
    products: [{
      id: 'threads',
      name: 'Space history',
      logo: LOGOS.googlechat,
      blurb: 'Questions and the replies that resolved them.',
      feeds: ['Questions and the replies that resolved them'],
      units: ['threads'],
    }],
    authorizeUrl: (clientId, state) => oauthUrl('https://accounts.google.com/o/oauth2/v2/auth', {
      client_id: clientId,
      response_type: 'code',
      scope: 'https://www.googleapis.com/auth/chat.spaces.readonly',
      access_type: 'offline',
      state,
    }),
  },
  {
    id: 'crawl',
    kind: 'Websites',
    name: 'Website crawl',
    line: '#8B8B94',
    logo: LOGOS.crawl,
    blurb: 'Recurring re-index of a public docs or help-center site.',
    products: [{
      id: 'pages',
      name: 'Crawled pages',
      logo: LOGOS.crawl,
      blurb: 'Pages found by following links from a starting URL.',
      feeds: ['Pages found on the site you point me at'],
      units: ['pages'],
    }],
    authorizeUrl: () => null,
  },
].map((s) => ({ ...s, comingSoon: true }));

/* --- Answering model: not a content source, just which LLM answers ------
   Kept separate from SOURCES: it has no sign-in, no crawl, and no feeds —
   toggling it on just means "this model is allowed to answer", gated by an
   API key entered once and never re-asked for. */

export const MODEL_SOURCE = {
  id: 'model',
  kind: 'Answering model',
  name: 'Answering model',
  line: '#8B8B94',
  blurb: 'Choose which model is allowed to answer questions.',
  products: [
    {
      id: 'gemini',
      name: 'Gemini',
      logo: LOGOS.gemini,
      blurb: 'Google’s model, called with your Gemini API key.',
      keyLabel: 'Gemini API key',
    },
    {
      id: 'claude',
      name: 'Claude',
      logo: LOGOS.claude,
      blurb: 'Anthropic’s model, called with your Anthropic API key.',
      keyLabel: 'Anthropic API key',
    },
  ],
};
