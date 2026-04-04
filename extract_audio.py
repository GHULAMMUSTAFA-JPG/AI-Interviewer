from pymongo import MongoClient
import gridfs

client = MongoClient('mongodb://mongodb:27017/?directConnection=true')
db = client['interviews']
fs = gridfs.GridFS(db, collection='audio')
cursor = db['audio.files'].find().sort('_id', -1).limit(1)
latest = list(cursor)
if latest:
    file_id = latest[0]['_id']
    data = fs.get(file_id)
    content = data.read()
    with open('/tmp/latest_audio.mp3', 'wb') as f:
        f.write(content)
    print(f'Saved {len(content)} bytes')
    print(f'Metadata: {latest[0].get("metadata", {})}')
else:
    print('No audio in GridFS')
