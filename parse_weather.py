import json
import re
from datetime import datetime

# Read the HTML file
with open('shanghai_today.html', 'r', encoding='utf-8') as f:
    html = f.read()

# Extract observe24h_data
m = re.search(r'var observe24h_data = ({.*?});', html, re.DOTALL)
if m:
    data = json.loads(m.group(1))
    od = data['od']
    print('Observation base time:', od['od0'])
    print('City:', od['od1'])
    print('-' * 60)
    print('Latest observations (last few hours):')
    for item in od['od2'][:5]:
        print(item)
else:
    print('No observe24h_data found')

# Extract forecast title
m2 = re.search(r'<input type="hidden" id="hidden_title" value="(.*?)"', html)
if m2:
    print('\nTitle:', m2.group(1))

# Extract today forecast
m3 = re.search(r'<p title="(.*?)" class="wea">', html)
if m3:
    print('Weather condition from HTML:', m3.group(1))

# Extract life indices
indices = re.findall(r'<li[^>]*>\s*<i[^>]*></i>\s*<span>(.*?)</span>\s*<em>(.*?)</em>', html)
print('\nLife indices:')
for val, name in indices[:10]:
    print(f'  {name}: {val}')
