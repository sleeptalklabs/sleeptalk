// Synthetic HPKE interoperability. The module path selects a pinned local install.
const {CipherSuite, DhkemP256HkdfSha256, HkdfSha256, Aes128Gcm}=require(process.env.SLEEPTALK_HPKE_MODULE);
(async()=>{
 let input=''; for await (const chunk of process.stdin) input+=chunk;
 const data=JSON.parse(input), suite=new CipherSuite({kem:new DhkemP256HkdfSha256(), kdf:new HkdfSha256(), aead:new Aes128Gcm()});
 const bytes=s=>Buffer.from(s,'base64url');
 const publicKey=await suite.kem.importKey('jwk',data.publicKey,true);
 const privateKey=await suite.kem.importKey('jwk',data.privateKey,false);
 const info=bytes(data.info);
 const sender=await suite.createSenderContext({recipientPublicKey:publicKey,info});
 const ct=await sender.seal(bytes(data.plaintext));
 const receiver=await suite.createRecipientContext({recipientKey:privateKey,enc:bytes(data.enc),info});
 const plaintext=await receiver.open(bytes(data.ct));
 if(!Buffer.from(plaintext).equals(bytes(data.plaintext))) throw Error('Python ciphertext did not round-trip');
 const wrong=await suite.createRecipientContext({recipientKey:privateKey,enc:bytes(data.enc),info:Buffer.from('wrong-context')});
 let rejected=false;try{await wrong.open(bytes(data.ct));}catch{rejected=true;}
 if(!rejected)throw Error('Changed context accepted');
 process.stdout.write(JSON.stringify({enc:Buffer.from(sender.enc).toString('base64url'),ct:Buffer.from(ct).toString('base64url')}));
})().catch(()=>{console.error('HPKE interoperability proof failed');process.exit(1);});
