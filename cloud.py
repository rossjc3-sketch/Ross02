"""Single-instance cloud entry point; never print the private access code."""
import os

if len(os.environ.get('QUICKCUT_TOKEN', '')) < 24:
    raise RuntimeError('Set QUICKCUT_TOKEN to a random secret of at least 24 characters.')
if not os.environ.get('QUICKCUT_DATA'):
    raise RuntimeError('Set QUICKCUT_DATA to the mounted persistent volume path.')

from app import app

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', '8080')),
                proxy_headers=True, forwarded_allow_ips='*', workers=1)
