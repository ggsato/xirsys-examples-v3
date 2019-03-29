<?php
    $curl = curl_init();
    $url = "https://global.xirsys.net/_turn/".$_POST["channel"];
    $expiration = "30";
    if(isset($_POST["expire"])) {
        $expiration = $_POST["expire"];
    };
    $data = array(
        'expire' => $expiration
    );
    $payload = json_encode($data);
    $header = array('Content-Type: application/json', 'Content-Length: ' . strlen($payload));
    curl_setopt_array( $curl, array (
        CURLOPT_URL => $url,
        CURLOPT_USERPWD => "ggsato:14f79f12-4161-11e9-b010-0242ac110003",
        CURLOPT_HTTPAUTH => CURLAUTH_BASIC,
        CURLOPT_CUSTOMREQUEST => "PUT",
        CURLOPT_RETURNTRANSFER => 1,
        CURLOPT_POSTFIELDS => $payload,
        CURLOPT_HTTPHEADER => $header
    ));
    $resp = curl_exec($curl);
    print $resp;
    curl_close($curl);
?>